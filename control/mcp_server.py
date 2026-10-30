#!/usr/bin/env python3
"""mcp_server.py — the operator control MCP: perception + macro execution +
desktop actions, as tools for the headless agent.

Spoken protocol: MCP over stdio (newline-delimited JSON-RPC 2.0), same as the
Playwright MCP the agent already uses. Registered per-run by operator_agent.py
via operator-mcp.sh, with the active surface in OPERATOR_SURFACE.

Tools:
    perceive    capture the active surface → labeled WorldState (targets via
                template/colour finders, text via OCR), optionally compiled
                from a per-game map (vision/maps/<game>.yaml), with grid /
                crop / annotated captures for grounding.
    game_macro  execute a multi-step macro locally (macro.py) — the
                planner/controller split. One LLM call in, many fast actions
                out, structured result back.
    computer    (desktop surfaces only) direct computer-use actions —
                screenshot / clicks / type / key / scroll / drag — on the Xvfb
                sandbox or (gated) the real Windows desktop. Browser surfaces
                don't get it: the Playwright MCP covers direct browser action.

Every call is traced into operator-events.ndjson so the cockpit shows the
macro's fast hands live.

Import layout: run via operator-mcp.sh, which puts vision/ and control/ on
PYTHONPATH — modules import flat (perceive, maps, overlay, macro, surfaces).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import operator_job_tools
try:
    import operator_overwatch
except ImportError:
    operator_overwatch = None  # type: ignore[assignment]
try:
    import operator_steer
except ImportError:
    operator_steer = None  # type: ignore[assignment]

import numpy as np
from PIL import Image

import emu_input as emu_input_mod
import events
import maps as maps_mod
import overlay
import perceive as perceive_mod
from macro import (MacroController, frame_change_frac,  # noqa: F401 (validate re-exported)
                   validate_macro)
from surfaces import (SurfaceError, SurfaceStopped, _load_cu_module,
                      get_surface, CDP_URL, SandboxSurface, RealDesktopSurface,
                      gui_lock)

PROTOCOL_FALLBACK = "2024-11-05"
_MAX_TARGETS = 40
_MAX_TEXT = 60

OUT_DIR = os.environ.get("COMPUTER_USE_OUTPUT_DIR",
                         os.path.expanduser("~/.cache/computer-use"))


# ── tool schemas ─────────────────────────────────────────────────────────────
_COND_NOTE = ("Conditions: {target,min_count?} | {no_target} | {text} | "
              "{count_delta:{label,cmp,value}} | "
              "{pixel_change:{region:[x,y,w,h],min_frac?}} | {all:[..]} | {any:[..]}")

_PERCEIVE_TOOL = {
    "name": "perceive",
    "description": (
        "Capture the active surface and return a labeled WorldState — targets "
        "found by template/colour matching and OCR'd text — so you ground on "
        "labels+coordinates instead of eyeballing pixels. Use a shipped per-game "
        "map (see vision/maps) and/or inline finder specs. Options: grid=true "
        "saves a coordinate-grid capture; region=[x,y,w,h] saves a full-res "
        "crop; annotate=true saves the capture with targets marked; "
        "return_image=true returns the fresh frame or requested crop/overlay inline."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "map": {"type": "string",
                    "description": "per-game map name, e.g. 'lichess', 'openrsc'"},
            "specs": {"type": "array", "items": {"type": "object"},
                      "description": ("inline finder specs: {kind:'color',label,lo,hi,"
                                      "min_area} | {kind:'text',label,region?,min_conf?} "
                                      "| {kind:'template',label,template_path,threshold?}")},
            "region": {"type": "array", "items": {"type": "integer"},
                       "description": "crop [x,y,w,h] to save at full res"},
            "grid": {"type": "boolean"},
            "annotate": {"type": "boolean"},
            "som": {"type": "boolean"},
            "return_image": {"type": "boolean"},
        },
    },
}

_GAME_MACRO_TOOL = {
    "name": "game_macro",
    "description": (
        "Execute a multi-step macro at machine speed with LOCAL perception "
        "between steps — no model round-trips mid-macro. Emit the whole plan "
        "(clicks by target label or xy, waits on conditions, repeats until a "
        "condition) and read the structured result; keep sequences short near "
        "uncertain game states. return_image=true captures a fresh final screenshot. "
        "It bails back to you on "
        "unmet verify, unknown target, watcher hit, budget, or yield. Ops: "
        "click_target{label,index?,verify?} click_xy{x,y} drag_xy{x1,y1,x2,y2} "
        "type{text,clear?} key{key} scroll{x,y,direction?} wait_ms{ms} "
        "wait_until{cond,timeout_s?,on_timeout?} repeat{ops,until,max?} "
        "assert{cond} yield_to_planner{reason}. "
        "COORDINATES: all xy coords and cond regions are in PERCEIVE frame "
        "space — derive them from perceive (its grid/targets/crop), NOT from "
        "browser_take_screenshot (different scale → misclicks). Prefer "
        "click_target by label over raw click_xy whenever perceive shows the "
        "target. TYPING INTO A FIELD THAT MAY ALREADY HOLD TEXT: pass "
        "type{clear:true} — it selects all and deletes first. Plain type "
        "APPENDS, so a retry lands on the end of whatever failed last time and "
        "the field fills with repeated fragments. "
        "Keep verify regions tight around the EXPECTED change (one "
        "square/button), not a broad area a wrong action could also disturb. "
        + _COND_NOTE),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ops": {"type": "array", "items": {"type": "object"}},
            "map": {"type": "string",
                    "description": "per-game map for click_target/cond perception"},
            "specs": {"type": "array", "items": {"type": "object"},
                      "description": "inline finder specs (same shapes as perceive)"},
            "watch": {"type": "array", "items": {"type": "object"},
                      "description": "guard conds checked between ops: [{label, cond}]"},
            "step_budget": {"type": "integer"},
            "wall_clock_s": {"type": "number"},
            "return_image": {"type": "boolean"},
        },
        "required": ["ops"],
    },
}

_COMPUTER_TOOL = {
    "name": "computer",
    "description": (
        "Direct desktop action on the active desktop surface (Xvfb sandbox or "
        "gated real desktop): screenshot / left_click / right_click / "
        "middle_click / double_click / triple_click / mouse_move / "
        "left_click_drag / type / key / scroll / wait. Consequential actions "
        "return a `verified` verdict (local before/after pixel check): "
        "verified.changed=false means the screen did NOT react — treat that as "
        "a miss; re-ground (screenshot/perceive) before acting again, never "
        "retry the same coords blind."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "coordinate": {"type": "array", "items": {"type": "integer"}},
            "start_coordinate": {"type": "array", "items": {"type": "integer"}},
            # flat aliases: some models emit x/y instead of the coordinate
            # array no matter what the schema says — declared here so its
            # calls validate, handled server-side by _coord_of()
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "start_x": {"type": "integer"}, "start_y": {"type": "integer"},
            "som": {"type": "integer"},
            "ref": {"type": "string"},
            "target": {"type": "string"},
            "text": {"type": "string"},
            "clear": {"type": "boolean"},
            "duration": {"type": "number"},
            "scroll_direction": {"type": "string"},
            "scroll_amount": {"type": "integer"},
        },
        "required": ["action"],
    },
}

_EMU_INPUT_TOOL = {
    "name": "emu_input",
    "description": (
        "Press game controller buttons on the EmulatorJS game running in the "
        "Operator sandbox browser (Pokémon Red, Emerald etc.). Sandbox only. Use this instead of keyboard "
        "or click tools to play an emulator game. This drives the controller "
        "input API directly. buttons: a, b, "
        "start, select, up, down, left, right, l, r. Pass one `button` or a "
        "`buttons` sequence. hold_ms is the press duration (default 120); for a "
        "held direction (walking) use a larger hold_ms (16 to 2000 ms). gap_ms defaults "
        "to 700 (0 to 2000 ms). Each sequence must finish within 10 seconds. Inspect the screen "
        "between inputs to see the result. Each call returns a screenshot. "
        "action=status inspects without pressing. At most 8 buttons per call. "
        "action=save_state or load_state uses checkpoint slot 0 to 3 (default 0). "
        "Checkpoints are kept in this game tab only; reload/close clears them. "
        "They never write to the game server. Open hosted games "
        "with ?nosave=1 to disable shared autosave/resume; do not use cloud saves."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"enum": ["press", "status", "save_state", "load_state"]},
            "slot": {"type": "integer", "minimum": 0, "maximum": 3},
            "button": {"type": "string"},
            "buttons": {"type": "array", "items": {"type": "string"}},
            "hold_ms": {"type": "integer"},
            "gap_ms": {"type": "integer"},
        },
    },
}

_SANDBOX_EXEC_TOOL = {
    "name": "sandbox_exec",
    "description": (
        "Run a bash command inside the Operator Linux sandbox container "
        "(Ubuntu 24.04 XFCE desktop on DISPLAY=:1, user opuser). Prefer this for "
        "fast CLI work, Python/Node scripts, data processing, "
        "ffmpeg/ImageMagick/LibreOffice conversions, and launching or inspecting tools. "
        "Conversation files live in /home/opuser/Workspace/<cid>."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout_s": {"type": "number"},
            "background": {"type": "boolean"},
        },
        "required": ["command"],
    },
}

_SANDBOX_WINDOW_TOOL = {
    "name": "sandbox_window",
    "description": (
        "Manage X11 windows and desktop applications on the Operator Linux sandbox "
        "desktop (DISPLAY=:1): list open windows, focus a window by id, launch an app "
        "(terminal, files, chromium, libreoffice, etc.), or reset the desktop."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "focus", "launch", "reset_desktop"],
            },
            "window_id": {"type": "string"},
            "app": {"type": "string"},
            "return_image": {"type": "boolean"},
        },
        "required": ["action"],
    },
}


_OVERWATCH_TOOL = {
    "name": "overwatch",
    "description": (
        "DVR temporal overwatch on the active surface: capture a "
        "multi-frame high-FPS burst with deterministic telemetry HUD burn-in "
        "(monotonic clock, action ID, crosshair reticle, lease/URL, XHR count), "
        "rolling 10s .webm/.mp4 DVR ring-buffer slices, and synchronized "
        "HAR/console/exception sidecar. "
        "action='burst' records across duration_ms and returns the peak-change "
        "transient keyframe + final frame inline. "
        "action='act_and_watch' dispatches trigger ('click'|'double_click'|"
        "'right_click'|'move'|'hover'|'drag'|'type'|'key'|'scroll') after "
        "frame 0 while recording the burst so 400ms error toasts, hover flyout "
        "menus, React hydration layout shifts, and canvas animations are "
        "captured at the exact millisecond they occur. "
        "action='replay' flushes the rolling DVR slices and sidecar anomalies."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["burst", "act_and_watch", "replay"],
            },
            "trigger": {
                "type": "string",
                "enum": [
                    "click", "double_click", "right_click", "move", "hover",
                    "drag", "type", "key", "scroll",
                ],
            },
            "coordinate": {"type": "array", "items": {"type": "integer"}},
            "start_coordinate": {"type": "array", "items": {"type": "integer"}},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "start_x": {"type": "integer"},
            "start_y": {"type": "integer"},
            "som": {"type": "integer"},
            "ref": {"type": "string"},
            "target": {"type": "string"},
            "text": {"type": "string"},
            "clear": {"type": "boolean"},
            "key": {"type": "string"},
            "direction": {"type": "string"},
            "amount": {"type": "integer"},
            "duration_ms": {"type": "integer"},
            "fps": {"type": "number"},
            "frames": {"type": "integer"},
            "region": {"type": "array", "items": {"type": "integer"}},
        },
    },
}


def _cap_ws(ws: dict) -> dict:
    """Bound the WorldState lists so a busy frame can't flood the context.
    Static targets (map-declared grid cells, e.g. 64 chess squares) are exempt
    from the detection cap — they're deliberate and compact, and capping them
    silently amputated the board (40 of 64 squares, no e1/e2...)."""
    out = dict(ws)
    targets = list(ws.get("targets", []))
    statics = [t for t in targets if t.get("kind") == "static"]
    detected = [t for t in targets if t.get("kind") != "static"]
    out["targets"] = detected[:_MAX_TARGETS] + statics
    out["text"] = list(ws.get("text", []))[:_MAX_TEXT]
    return out


def _b64_jpeg(arr: np.ndarray, quality: int = 80) -> str:
    import base64
    import io
    buf = io.BytesIO()
    Image.fromarray(arr).convert("RGB").save(buf, "JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode()


class OperatorMCP:
    """One server instance per agent run. The surface is created lazily on the
    first tool call (tools/list must work even if the surface can't come up)."""

    def __init__(self, surface_name: str = "", bot: str = "",
                 surface_factory=get_surface) -> None:
        self.surface_name = surface_name or os.environ.get(
            "OPERATOR_SURFACE", "browser")
        self.bot = bot or os.environ.get("OPERATOR_BOT", "") or "operator"
        self._factory = surface_factory
        self._surface = None
        self._emu = None            # lazy EmuInput (sandbox game harness)
        self._emu_tunnel = None     # host-loopback tunnel owned by this MCP
        self._overwatch_ring = None
        self._overwatch_sidecar = None
        self._overwatch_session = None
        self._overwatch_adapter = None
        self._overwatch_cdp_attached = False
        self._overwatch_start_mono = time.monotonic()
        self._overwatch_action_seq = 0
        self._last_som_elements: list[dict] = []

    # -- surface / perception ------------------------------------------------
    def _get_surface(self):
        if self._surface is None:
            if self.surface_name == "auto":
                if (os.environ.get("OPERATOR_REAL_OK") == "1"
                        and os.environ.get("OPERATOR_ACTIVE_SURFACE") == "desktop-real"):
                    self._surface = RealDesktopSurface(real_ok=True)
                else:
                    self._surface = SandboxSurface()
            else:
                self._surface = self._factory(self.surface_name)
        return self._surface

    def _compile_spec(self, args: dict, frame_size: tuple) -> list:
        spec: list = []
        if args.get("map"):
            m = maps_mod.load_map(maps_mod.map_path(args["map"]))
            spec.extend(maps_mod.spec_from_map(m, frame_size))
        for s in args.get("specs") or []:
            s = dict(s)
            if s.get("kind") == "template" and s.get("template_path"):
                p = os.path.expanduser(s.pop("template_path"))
                if not os.path.exists(p):
                    continue
                s["template"] = Image.open(p).convert("RGB")
            if s.get("kind") == "color":
                s["lo"] = tuple(s["lo"])
                s["hi"] = tuple(s["hi"])
            if s.get("kind") == "text" and isinstance(s.get("region"), list):
                s["region"] = tuple(s["region"])
            spec.append(s)
        return spec

    def _perceive_fn(self, args: dict):
        """perceive(frame) closure for the macro controller — spec compiled once
        per frame size, cached."""
        cache: dict = {}

        def _p(frame: np.ndarray) -> dict:
            size = (frame.shape[1], frame.shape[0])
            if size not in cache:
                cache[size] = self._compile_spec(args, size)
            try:
                return perceive_mod.build_world_state(frame, cache[size]).as_dict()
            except RuntimeError:
                # OCR unavailable (no pytesseract) must degrade, not kill the
                # macro — drop text specs and keep the target finders working.
                cache[size] = [s for s in cache[size] if s.get("kind") != "text"]
                return perceive_mod.build_world_state(frame, cache[size]).as_dict()
        return _p

    # -- tool handlers ---------------------------------------------------------
    def _tool_perceive(self, args: dict) -> dict:
        surface = self._get_surface()
        frame = surface.frame()
        size = (frame.shape[1], frame.shape[0])
        spec = self._compile_spec(args, size)
        if not spec:
            # no map/specs → default to a full-frame OCR pass (best-effort:
            # boxes need tesseract; targets stay empty without finder specs)
            spec = [{"kind": "text", "label": "text"}]
        try:
            ws = perceive_mod.build_world_state(frame, spec).as_dict()
        except RuntimeError as e:          # OCR unavailable — still useful
            ws = {"targets": [], "text": [], "w": size[0], "h": size[1],
                  "note": str(e)}
        out: dict = {"world_state": _cap_ws(ws), "surface": self.surface_name}
        img = None
        if args.get("som"):
            som_items: list[dict] = []
            if hasattr(surface, "interactive_elements"):
                try:
                    for dom_it in surface.interactive_elements(limit=40):
                        if len(som_items) >= 40:
                            break
                        entry = {
                            "id": len(som_items) + 1,
                            "label": str(dom_it.get("label") or "element"),
                            "x": int(dom_it.get("x", 0)),
                            "y": int(dom_it.get("y", 0)),
                            "w": int(dom_it.get("w", 16)),
                            "h": int(dom_it.get("h", 16)),
                        }
                        if dom_it.get("ref"):
                            entry["ref"] = str(dom_it["ref"])
                        som_items.append(entry)
                except Exception:
                    pass
            cws = out["world_state"]
            for item in list(cws.get("targets", [])):
                if len(som_items) >= 40:
                    break
                som_items.append({
                    "id": len(som_items) + 1,
                    "label": str(item.get("label") or item.get("text") or ""),
                    "x": int(item.get("x", 0)),
                    "y": int(item.get("y", 0)),
                    "w": int(item.get("w", 0)),
                    "h": int(item.get("h", 0)),
                })
            for item in list(cws.get("text", [])):
                if len(som_items) >= 40:
                    break
                w_val = int(item.get("w", 0))
                h_val = int(item.get("h", 0))
                cx = int(item.get("x", 0)) + (w_val // 2 if w_val > 0 else 0)
                cy = int(item.get("y", 0)) + (h_val // 2 if h_val > 0 else 0)
                if any(abs(existing["x"] - cx) < 16 and abs(existing["y"] - cy) < 16 for existing in som_items):
                    continue
                raw_lbl = item.get("label")
                lbl = str(item.get("text") if raw_lbl in (None, "", "text") else (raw_lbl or item.get("text") or ""))
                som_items.append({
                    "id": len(som_items) + 1,
                    "label": lbl,
                    "x": cx,
                    "y": cy,
                    "w": w_val,
                    "h": h_val,
                })
            self._last_som_elements = som_items
            out["som_elements"] = som_items
            img = np.asarray(overlay.draw_som(frame, som_items))
        elif args.get("region"):
            img = np.asarray(overlay.crop_region(frame, args["region"]))
        elif args.get("grid"):
            img = np.asarray(overlay.draw_grid(frame))
        elif args.get("annotate"):
            img = np.asarray(overlay.annotate_targets(frame, ws.get("targets", [])))
        if img is None and args.get("return_image"):
            img = frame
        if img is not None:
            os.makedirs(OUT_DIR, exist_ok=True)
            path = os.path.join(OUT_DIR, f"perceive-{int(time.time()*1000)}.jpg")
            Image.fromarray(img).convert("RGB").save(path, "JPEG", quality=85)
            out["saved"] = path
        content = [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]
        if img is not None and args.get("return_image"):
            content.append({"type": "image", "data": _b64_jpeg(img),
                            "mimeType": "image/jpeg"})
        events.record(self.bot, "perceive", "Perceiving",
                      f"{len(ws.get('targets', []))} targets, "
                      f"{len(ws.get('text', []))} text")
        return {"content": content, "isError": False}

    def _tool_game_macro(self, args: dict) -> dict:
        surface = self._get_surface()
        ops = args.get("ops") or []
        events.record(self.bot, "game_macro", "Running macro", f"{len(ops)} ops")
        ctl = MacroController(
            surface,
            perceive=self._perceive_fn(args),
            on_event=lambda label, detail: events.record(
                self.bot, "game_macro", label, detail, throttle=True),
            step_budget=int(args.get("step_budget", 300)),
            wall_clock_s=float(args.get("wall_clock_s", 240.0)))
        result = ctl.run(ops, watch=args.get("watch"))
        result["world_state"] = _cap_ws(result.get("world_state") or {})
        events.record(self.bot, "game_macro",
                      "Macro done" if result["done"] else "Macro yielded",
                      result.get("stopped_reason") or
                      f"{result['steps_executed']} steps")
        content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
        if args.get("return_image"):
            # Capture after the macro, including a yield/failure, so the model
            # observes the current surface rather than the pre-action frame.
            content.append({"type": "image", "data": _b64_jpeg(surface.frame()),
                            "mimeType": "image/jpeg"})
        return {"content": content, "isError": False}

    # v1.1 §3.1 — step-level verify: after a consequential action, compare a
    # before/after frame LOCALLY (no model call) and put the verdict in the
    # tool result. The old verify-after-action was an advisory directive the
    # model could skip; this is structural — every click/type/key/scroll
    # answer carries ground truth ("did anything actually change?"), so
    # "clicked empty space and ploughed on" is visible the moment it happens.
    _VERIFY_ACTIONS = frozenset((
        "left_click", "right_click", "middle_click", "double_click",
        "triple_click", "left_click_drag", "type", "key", "scroll"))
    _VERIFY_BOX = 90          # half-size of the click-verify region (px)
    _LOOK_BOX = 180           # half-size of the failed-click zoom crop (px)

    @staticmethod
    def _verify_params(action: str, coord) -> tuple:
        """(region, min_frac) for the change check. Clicks look at a tight box
        around the click point; type/key/scroll look at the whole frame with a
        near-zero floor (a typed char is a tiny fraction of the screen)."""
        if action.endswith("_click") or action == "left_click_drag":
            half = OperatorMCP._VERIFY_BOX
            return ([int(coord[0]) - half, int(coord[1]) - half,
                     2 * half, 2 * half], 0.02)
        return (None, 0.0005)

    def _verify_action(self, surface, action: str, coord, pre_frame) -> dict | None:
        """Best-effort — a verify failure must never break the action result."""
        try:
            settle = max(0.0, min(float(os.environ.get("OPERATOR_VERIFY_SETTLE_S", "0.4")), 3.0))
            region, min_frac = self._verify_params(action, coord)
            deadline = time.monotonic() + settle
            step = min(0.08, settle) if settle > 0 else 0.0
            frac = None
            while True:
                if step > 0:
                    time.sleep(step)
                post = surface.frame()
                frac = frame_change_frac(pre_frame, post, region)
                if frac is None and region is not None:   # box fell off-frame
                    frac = frame_change_frac(pre_frame, post)
                if frac is not None and frac >= min_frac:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                step = min(0.08, remaining)
            if frac is None:
                return None
            out = {"changed": frac >= min_frac, "change_frac": round(frac, 4),
                   "scope": "region" if region is not None else "frame"}
            if not out["changed"]:
                out["hint"] = (
                    "no visible change after this action — it may have missed "
                    "(wrong coords, a covered target, an unfocused window). Do "
                    "NOT retry the same action blind: screenshot or perceive "
                    "first, diagnose, then act differently.")
            return out
        except Exception:  # noqa: BLE001
            return None

    # v1.1 §3.2a — auto-perceive on a failed click verify: the model's next
    # move after "no visible change" used to be a blind re-click of the same
    # coords. Give it ground truth IN THE SAME tool result instead: a zoomed
    # crop around the click point (2× upscaled for legibility) plus, when
    # tesseract is present, the OCR'd text near the click with full-frame
    # coordinates — so the retry is corrected, not guessed. Best-effort;
    # kill-switch OPERATOR_AUTO_LOOK=0.
    def _look_closer(self, surface, coord) -> tuple:
        """(crop_ndarray | None, info dict | None) for a failed click."""
        try:
            frame = surface.frame()
            h, w = frame.shape[:2]
            half = self._LOOK_BOX
            x0, y0 = max(0, int(coord[0]) - half), max(0, int(coord[1]) - half)
            x1, y1 = min(w, int(coord[0]) + half), min(h, int(coord[1]) + half)
            if x1 - x0 < 8 or y1 - y0 < 8:
                return None, None
            crop = frame[y0:y1, x0:x1]
            img = Image.fromarray(crop).convert("RGB")
            img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
            info: dict = {"crop_region": [x0, y0, x1 - x0, y1 - y0]}
            try:
                hits = perceive_mod.read_text(
                    frame, region=(x0, y0, x1 - x0, y1 - y0))
                info["text"] = [{"text": t.text, "x": int(t.x), "y": int(t.y)}
                                for t in hits[:12]]
            except RuntimeError:
                pass   # no tesseract on the host — the crop alone still helps
            return np.asarray(img), info
        except Exception:  # noqa: BLE001 — a failed look must not break the action
            return None, None

    @staticmethod
    def _coord_of(args: dict, key: str = "coordinate",
                  xk: str = "x", yk: str = "y") -> list | None:
        """Extract a click point from EITHER arg shape. The schema says
        `coordinate: [x, y]` and Claude follows it — but some models emit
        flat `x`/`y` integer fields instead, and the old
        `args.get("coordinate") or [0, 0]` silently turned every flat-arg click
        into (0, 0). Returns None
        when neither shape is present so callers can fail LOUDLY."""
        c = args.get(key)
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            try:
                return [int(c[0]), int(c[1])]
            except (TypeError, ValueError):
                return None
        if xk in args and yk in args:
            try:
                return [int(args[xk]), int(args[yk])]
            except (TypeError, ValueError):
                return None
        return None

    _COORD_ACTIONS = ("left_click", "right_click", "middle_click",
                      "double_click", "triple_click", "mouse_move",
                      "left_click_drag")

    def _tool_computer(self, args: dict) -> dict:
        with gui_lock(self.surface_name):
            if self.surface_name == "browser":
                raise SurfaceError(
                    "the computer tool is for desktop surfaces — on the browser "  # copy-ok
                    "surface use your Playwright browser tools")
            surface = self._get_surface()
            action = args.get("action", "")
            coord = self._resolve_overwatch_coord(surface, args)
            has_explicit_coord = coord is not None
            if coord is None and action in self._COORD_ACTIONS:
                # never fall back to (0, 0): a corner-click LOOKS like a dead
                # sandbox and burns the whole run before anyone suspects the args
                raise SurfaceError(
                    f"{action} needs coordinates — pass coordinate: [x, y]")  # copy-ok
            if coord is None:
                coord = [0, 0]   # scroll / type may legitimately omit a position
            label, detail = "Acting", action
            pre_frame = None
            if (action in self._VERIFY_ACTIONS
                    and os.environ.get("OPERATOR_STEP_VERIFY", "1") != "0"):
                try:
                    pre_frame = surface.frame()
                except Exception:  # noqa: BLE001 — verify is best-effort
                    pre_frame = None
            if action == "screenshot":
                frame = surface.frame()
                events.record(self.bot, "computer", "Capturing",
                              f"{frame.shape[1]}×{frame.shape[0]}")
                return {"content": [
                    {"type": "text",
                     "text": json.dumps({"w": int(frame.shape[1]),
                                         "h": int(frame.shape[0])})},
                    {"type": "image", "data": _b64_jpeg(frame),
                     "mimeType": "image/jpeg"}], "isError": False}
            if action in ("left_click", "right_click", "middle_click",
                          "double_click", "triple_click"):
                button = {"right_click": "right",
                          "middle_click": "middle"}.get(action, "left")
                clicks = {"double_click": 2, "triple_click": 3}.get(action, 1)
                surface.click(int(coord[0]), int(coord[1]), button=button,
                              clicks=clicks)
                label, detail = "Clicking", f"({coord[0]}, {coord[1]})"
            elif action == "mouse_move":
                surface.move(int(coord[0]), int(coord[1]))
                label = "Moving"
            elif action == "left_click_drag":
                start = self._coord_of(args, key="start_coordinate",
                                       xk="start_x", yk="start_y")
                if start is None:
                    raise SurfaceError("left_click_drag needs start_coordinate: "
                                       "[x, y] plus coordinate: [x, y] for the end")
                surface.drag(int(start[0]), int(start[1]),
                             int(coord[0]), int(coord[1]))
                label, detail = "Dragging", f"({start[0]}, {start[1]}) → ({coord[0]}, {coord[1]})"
            elif action == "type":
                if has_explicit_coord:
                    surface.click(int(coord[0]), int(coord[1]), button="left", clicks=1)
                if args.get("clear"):
                    surface.key("ctrl+a")
                    surface.key("Delete")
                surface.type_text(args.get("text", ""))
                label, detail = "Typing", str(args.get("text", ""))[:60]
            elif action == "key":
                surface.key(args.get("text", ""))
                label, detail = "Pressing", str(args.get("text", ""))
            elif action == "scroll":
                s_dir = args.get("scroll_direction") or args.get("direction") or "down"
                s_amt = int(args.get("scroll_amount") or args.get("amount") or 3)
                surface.scroll(int(coord[0]), int(coord[1]),
                               direction=str(s_dir),
                               amount=s_amt)
                label = "Scrolling"
            elif action == "wait":
                time.sleep(min(float(args.get("duration", 1.0)), 10.0))
                label = "Waiting"
            else:
                raise SurfaceError(f"unsupported computer action: {action!r}")
            out: dict = {"ok": True}
            look_img = None
            if pre_frame is not None:
                verdict = self._verify_action(surface, action, coord, pre_frame)
                if verdict is not None:
                    out["verified"] = verdict
                    if not verdict["changed"]:
                        events.record(self.bot, "computer", "No change",
                                      f"{action} at ({coord[0]}, {coord[1]})")
                        # §3.2a: failed CLICKS get an automatic close look — the
                        # actions where "re-aim" (vs "re-think") is the likely fix.
                        if ((action.endswith("_click") or action == "left_click_drag")
                                and os.environ.get("OPERATOR_AUTO_LOOK", "1") != "0"):
                            look_img, look = self._look_closer(surface, coord)
                            if look is not None:
                                verdict["look"] = look
                                verdict["hint"] = (
                                    "no visible change after this click — it likely "  # copy-ok
                                    "missed (wrong coords, covered target, unfocused "
                                    "window). A 2×-zoomed screenshot of the click "
                                    "area is attached; verified.look.crop_region is "  # copy-ok
                                    "its full-frame position"
                                    + (" and verified.look.text lists on-screen text "
                                       "near your click with FULL-FRAME coordinates"
                                       if look.get("text") else "")
                                    + ". Find the real target and correct your "
                                      "coordinates — do NOT re-click the same spot.")  # copy-ok
                                events.record(self.bot, "computer", "Looking closer",
                                              f"crop at ({coord[0]}, {coord[1]})")
            events.record(self.bot, "computer", label, detail)
            content = [{"type": "text", "text": json.dumps(out)}]
            if look_img is not None:
                content.append({"type": "image", "data": _b64_jpeg(look_img),
                                "mimeType": "image/jpeg"})
            return {"content": content, "isError": False}

    _handle_computer = _tool_computer

    def _handle_sandbox_exec(self, args: dict) -> dict:
        sb = _load_cu_module("sandbox_container.py")
        command = args["command"]
        cwd = args.get("cwd", "")
        timeout = float(args.get("timeout_s") or 60.0)
        cid = os.environ.get("OPERATOR_CONVERSATION_ID", "")
        background = bool(args.get("background", False))

        label = "Starting background app in Linux" if background else "Running in Linux"
        events.record(self.bot, "sandbox_exec", label, command[:120])

        res = sb.exec_command(
            command=command, cwd=cwd, timeout=timeout, cid=cid, background=background)
        return {"content": [{"type": "text", "text": json.dumps(res)}], "isError": False}

    _tool_sandbox_exec = _handle_sandbox_exec

    def _handle_sandbox_window(self, args: dict) -> dict:
        sb = _load_cu_module("sandbox_container.py")
        action = str(args.get("action") or "").lower()
        return_image = args.get("return_image")
        if return_image is None:
            return_image = action in ("focus", "launch", "reset_desktop")
        else:
            return_image = bool(return_image)

        if action == "list":
            events.record(self.bot, "sandbox_window", "Checking Linux windows", "")
            windows = sb.list_windows()
            res = {"ok": True, "windows": windows}
            return {"content": [{"type": "text", "text": json.dumps(res)}], "isError": False}

        if action == "focus":
            wid = str(args.get("window_id") or "")
            events.record(self.bot, "sandbox_window", "Switching Linux window", wid)
            ok = sb.focus_window(wid)
            res = {"ok": ok, "window_id": wid}
        elif action == "launch":
            app = str(args.get("app") or "xfce4-terminal")
            events.record(self.bot, "sandbox_window", "Opening Linux app", app)
            sb.launch(app)
            time.sleep(0.6)
            windows = sb.list_windows()
            res = {"ok": True, "app": app, "windows": windows}
        elif action == "reset_desktop":
            events.record(self.bot, "sandbox_window", "Resetting Linux desktop", "")
            res = sb.reset_desktop()
        else:
            raise SurfaceError(f"unknown sandbox_window action: {action!r}")

        content = [{"type": "text", "text": json.dumps(res)}]
        if return_image:
            try:
                frame = self._get_surface().frame()
                content.append({"type": "image", "data": _b64_jpeg(frame), "mimeType": "image/jpeg"})
            except Exception:
                pass
        return {"content": content, "isError": False}

    _tool_sandbox_window = _handle_sandbox_window

    def _get_emu(self):
        """Attach only to the isolated sandbox browser."""
        if self.surface_name not in ("desktop-sandbox", "auto"):
            raise SurfaceError("emulator controls require the Sandbox surface")
        if self._emu is None:
            sb = _load_cu_module("sandbox_container.py")
            sb.ensure()
            tunnel = sb.open_cdp_tunnel()
            try:
                emu = emu_input_mod.EmuInput(
                    tunnel.url, check_stop=self._get_surface()._check_stop)
            except Exception:
                tunnel.close()
                raise
            self._emu_tunnel = tunnel
            self._emu = emu
        return self._emu

    def close(self) -> None:
        """Release per-run transport resources without stopping the sandbox."""
        if (
            self._overwatch_session is not None
            and self._surface is not None
            and hasattr(self._surface, "detach_overwatch_session")
        ):
            try:
                self._surface.detach_overwatch_session(self._overwatch_session)
            except Exception:
                pass
        self._overwatch_session = None
        self._overwatch_adapter = None
        self._overwatch_cdp_attached = False
        if self._overwatch_ring is not None:
            try:
                self._overwatch_ring.close()
            except Exception:
                pass
            self._overwatch_ring = None
        if self._emu_tunnel is not None:
            self._emu_tunnel.close()
            self._emu_tunnel = None
        if self._emu is not None:
            self._emu.close()
        self._emu = None

    def _ensure_overwatch(self, fps: float = 8.0):
        if operator_overwatch is None:
            raise SurfaceError("operator_overwatch module is unavailable")
        if self._overwatch_ring is None:
            cid = os.environ.get("OPERATOR_CONVERSATION_ID") or self.bot or "default"
            ring_dir = os.path.join(OUT_DIR, "overwatch-mcp")
            fps_int = max(1, min(int(round(fps)), 15))
            self._overwatch_ring = operator_overwatch.OverwatchRingBuffer(
                cid,
                root_dir=ring_dir,
                fps=fps_int,
                slice_duration_s=10.0,
                max_slices=6,
            )
            self._overwatch_sidecar = operator_overwatch.SidecarRingBuffer()
            self._overwatch_session = operator_overwatch.CDPOverwatchSession(
                cid,
                ring=self._overwatch_ring,
                sidecar=self._overwatch_sidecar,
                fps=fps_int,
            )
            self._overwatch_adapter = operator_overwatch.ArgonOverwatchAdapter(
                self._overwatch_session,
                model=os.environ.get("OPERATOR_RUN_MODEL") or "gemini-4-argon",
            )
        if (
            not self._overwatch_cdp_attached
            and self._overwatch_session is not None
            and self._surface is not None
            and hasattr(self._surface, "attach_overwatch_session")
        ):
            self._overwatch_cdp_attached = bool(
                self._surface.attach_overwatch_session(self._overwatch_session)
            )
        return self._overwatch_ring, self._overwatch_sidecar

    def _stamp_and_record_frame(
        self,
        frame: np.ndarray,
        action_id: str,
        action_name: str,
        coord: list[int] | None,
        url_path: str,
    ) -> tuple[bytes, int]:
        ring, sidecar = self._ensure_overwatch()
        mono_ms = sidecar.current_offset_ms()
        buf = __import__("io").BytesIO()
        Image.fromarray(frame).convert("RGB").save(buf, "JPEG", quality=85)
        raw_jpeg = buf.getvalue()
        telemetry = operator_overwatch.FrameTelemetry(
            t_offset_ms=mono_ms,
            wall_utc_iso=operator_overwatch.datetime.now(
                operator_overwatch.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            action_id=action_id,
            action_name=action_name,
            target_xy=(int(coord[0]), int(coord[1])) if coord else None,
            lease_state="active",
            url_path=url_path or f"/{self.surface_name}",
            in_flight_xhr=sidecar.in_flight_xhr if sidecar else 0,
        )
        stamped = operator_overwatch.stamp_hud_frame(raw_jpeg, telemetry)
        ring.push_frame(raw_jpeg, telemetry, pre_stamped=stamped)
        return stamped, mono_ms

    def _resolve_overwatch_coord(self, surface, args: dict) -> list[int] | None:
        coord = self._coord_of(args)
        if coord is not None:
            return coord
        som_id = args.get("som")
        ref_id = str(args.get("ref") or "").strip().lower()
        target_lbl = str(args.get("target") or "").strip().lower()
        if som_id is None and not ref_id and not target_lbl:
            return None
        candidates = list(self._last_som_elements)
        if not candidates and hasattr(surface, "interactive_elements"):
            try:
                candidates = surface.interactive_elements(limit=40)
                self._last_som_elements = candidates
            except Exception:
                candidates = []
        if som_id is not None:
            try:
                sid = int(som_id)
                for item in candidates:
                    if int(item.get("id", -1)) == sid:
                        return [int(item["x"]), int(item["y"])]
            except (ValueError, TypeError):
                pass
        lookup_ref = ref_id or (target_lbl if re.match(r"^(?:f\d+)?e\d+$", target_lbl) else "")
        if lookup_ref:
            for item in candidates:
                if str(item.get("ref") or "").strip().lower() == lookup_ref:
                    return [int(item["x"]), int(item["y"])]
        if target_lbl:
            for item in candidates:
                lbl = str(item.get("label") or "").lower()
                if lbl == target_lbl or target_lbl in lbl:
                    return [int(item["x"]), int(item["y"])]
        return None

    def _dispatch_overwatch_trigger(
        self, surface, trigger: str, args: dict, pre_frame: np.ndarray | None = None
    ) -> tuple[str, list[int] | None]:
        self._overwatch_action_seq += 1
        act_id = f"act#{self._overwatch_action_seq:02d}"
        coord = self._resolve_overwatch_coord(surface, args)
        if trigger in ("click", "double_click", "right_click", "move", "hover", "drag"):
            if coord is None:
                raise SurfaceError(
                    f"overwatch trigger {trigger!r} needs coordinates (x, y, coordinate: [x, y], som, or target)"
                )
        if trigger in ("click", "double_click", "right_click"):
            btn = "right" if trigger == "right_click" else "left"
            clicks = 2 if trigger == "double_click" else 1
            surface.click(int(coord[0]), int(coord[1]), button=btn, clicks=clicks)
        elif trigger in ("move", "hover"):
            surface.move(int(coord[0]), int(coord[1]))
        elif trigger == "drag":
            start = self._coord_of(args, key="start_coordinate", xk="start_x", yk="start_y")
            if start is None:
                raise SurfaceError("overwatch drag needs start_x/start_y and x/y")
            surface.drag(int(start[0]), int(start[1]), int(coord[0]), int(coord[1]))
        elif trigger == "type":
            txt = str(args.get("text") or "")
            if not txt:
                raise ValueError("overwatch trigger 'type' requires non-empty text")
            if coord is not None:
                surface.click(int(coord[0]), int(coord[1]), button="left", clicks=1)
            if args.get("clear"):
                surface.key("ctrl+a")
                surface.key("Delete")
            surface.type_text(txt)
        elif trigger == "key":
            k = str(args.get("key") or args.get("text") or "")
            if not k:
                raise ValueError("overwatch trigger 'key' requires non-empty key")
            surface.key(k)
        elif trigger == "scroll":
            if coord is None:
                if pre_frame is not None and hasattr(pre_frame, "shape") and len(pre_frame.shape) >= 2:
                    coord = [int(pre_frame.shape[1]) // 2, int(pre_frame.shape[0]) // 2]
                else:
                    coord = [640, 400]
            surface.scroll(
                int(coord[0]),
                int(coord[1]),
                direction=str(args.get("direction", "down")),
                amount=int(args.get("amount", 3)),
            )
        else:
            raise SurfaceError(f"unsupported overwatch trigger: {trigger!r}")
        if self._overwatch_session is not None:
            self._overwatch_session.record_action(
                act_id,
                trigger,
                target_xy=(int(coord[0]), int(coord[1])) if coord else None,
            )
        return act_id, coord

    def _tool_overwatch(self, args: dict) -> dict:
        import base64
        action = str(args.get("action") or "burst")
        if action not in ("burst", "act_and_watch", "replay"):
            raise ValueError(f"unknown overwatch action: {action!r}")
        fps = max(1.0, min(float(args.get("fps") or 8.0), 15.0))
        surface = self._get_surface()
        ring, sidecar = self._ensure_overwatch(fps=fps)
        if action == "replay":
            bundle = (
                self._overwatch_adapter.flush_overwatch_bundle(reason="replay", duration_s=15.0)
                if self._overwatch_adapter is not None
                else None
            )
            slices = bundle.clip_paths if bundle else [s.path for s in ring.recent_slices(duration_s=15.0, flush=True)]
            events.record(self.bot, "overwatch", "Replaying DVR", f"{len(slices)} slices")
            sidecar_snap = bundle.sidecar if bundle else sidecar.window_snapshot(duration_s=15.0)
            if operator_steer is not None and hasattr(operator_steer, "take_anomalies"):
                anom = operator_steer.take_anomalies()
                if anom:
                    sidecar_snap["browser_anomalies"] = anom
            out = {
                "ok": True,
                "action": "replay",
                "dvr_slices": slices,
                "sidecar": sidecar_snap,
                "session_turn": bundle.session_turn if bundle else 1,
                "estimated_tokens": bundle.estimated_tokens if bundle else 0,
                "diagnosis_schema": [
                    "exact_failure_timestamp_ms",
                    "visual_causal_trace",
                    "corrective_action_spec",
                ],
                "prompt_directive": bundle.prompt_directive if bundle else "",
            }
            return {
                "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                "isError": False,
            }
        duration_ms = max(0, min(int(args.get("duration_ms", 600)), 5000))
        explicit_frames = args.get("frames")
        if explicit_frames is not None:
            n_post_frames = max(1, min(int(explicit_frames), 30))
        else:
            n_post_frames = max(2, min(int(round((duration_ms / 1000.0) * fps)) + 1, 24))
        interval_s = (duration_ms / 1000.0) / max(1, n_post_frames) if duration_ms > 0 else 0.0
        region = args.get("region")
        if isinstance(region, list) and len(region) == 4:
            region = [int(v) for v in region]
        else:
            region = None

        url_path = f"/{self.surface_name}"
        if getattr(surface, "_page", None) is not None:
            try:
                url_path = operator_overwatch.urlparse(surface._page.url).path or "/"
            except Exception:
                pass

        window_start_ms = sidecar.current_offset_ms()
        pre_frame = surface.frame()
        coord = self._resolve_overwatch_coord(surface, args)
        act_id = f"act#{self._overwatch_action_seq:02d}"
        act_name = "observe"
        pre_jpeg, pre_ms = self._stamp_and_record_frame(pre_frame, act_id, act_name, coord, url_path)

        trigger = str(args.get("trigger") or "")
        if action == "act_and_watch":
            if not trigger:
                raise ValueError("act_and_watch requires a trigger (e.g. 'click', 'hover', 'type', 'key', 'scroll')")
            act_id, coord = self._dispatch_overwatch_trigger(surface, trigger, args, pre_frame=pre_frame)
            act_name = trigger

        frames_raw = [pre_frame]
        frames_jpeg = [pre_jpeg]
        frames_ms = [pre_ms]
        step_deltas = [0.0]
        cum_deltas = [0.0]

        for idx in range(n_post_frames):
            if interval_s > 0:
                time.sleep(min(interval_s, 0.5))
            curr = surface.frame()
            stamped_jpeg, mono_ms = self._stamp_and_record_frame(curr, act_id, act_name, coord, url_path)
            s_delta = frame_change_frac(frames_raw[-1], curr, region)
            if s_delta is None and region is not None:
                s_delta = frame_change_frac(frames_raw[-1], curr)
            c_delta = frame_change_frac(pre_frame, curr, region)
            if c_delta is None and region is not None:
                c_delta = frame_change_frac(pre_frame, curr)
            frames_raw.append(curr)
            frames_jpeg.append(stamped_jpeg)
            frames_ms.append(mono_ms)
            step_deltas.append(float(s_delta or 0.0))
            cum_deltas.append(float(c_delta or 0.0))

        should_flush = not ring.active_slices() or len(ring._current_frames) >= max(4, ring.fps * 2)
        slices = [s.path for s in ring.recent_slices(duration_s=15.0, flush=should_flush)]
        peak_idx = max(range(1, len(frames_raw)), key=lambda i: max(step_deltas[i], cum_deltas[i]))
        peak_frac = max(step_deltas[peak_idx], cum_deltas[peak_idx])
        settled_frac = cum_deltas[-1]
        window_end_ms = frames_ms[-1]

        sidecar_snap = sidecar.window_snapshot(duration_s=15.0)
        if operator_steer is not None and hasattr(operator_steer, "take_anomalies"):
            anom = operator_steer.take_anomalies()
            if anom:
                sidecar_snap["browser_anomalies"] = anom

        out = {
            "ok": True,
            "action": action,
            "trigger": trigger or None,
            "frames_captured": len(frames_raw),
            "duration_ms": round(float(window_end_ms - window_start_ms), 1),
            "peak_change_frac": round(peak_frac, 4),
            "settled_change_frac": round(settled_frac, 4),
            "peak_frame_index": peak_idx,
            "peak_timestamp_ms": round(float(frames_ms[peak_idx]), 1),
            "transient_detected": bool(peak_frac >= 0.005),
            "transient_vanished": bool(peak_frac >= 0.01 and settled_frac < peak_frac * 0.5),
            "dvr_slices": slices,
            "sidecar": sidecar_snap,
        }
        events.record(
            self.bot,
            "overwatch",
            "Overwatch act+watch" if action == "act_and_watch" else "Overwatch burst",
            f"{len(frames_raw)}f peak={peak_frac:.3f}",
        )
        content = [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]
        if peak_idx != len(frames_jpeg) - 1 and peak_frac >= 0.005:
            content.append({
                "type": "image",
                "data": base64.standard_b64encode(frames_jpeg[peak_idx]).decode(),
                "mimeType": "image/jpeg",
            })
        content.append({
            "type": "image",
            "data": base64.standard_b64encode(frames_jpeg[-1]).decode(),
            "mimeType": "image/jpeg",
        })
        return {"content": content, "isError": False}

    def _tool_emu_input(self, args: dict) -> dict:
        if self.surface_name not in ("desktop-sandbox", "auto"):
            raise SurfaceError("emu_input requires the Sandbox surface")
        action = args.get("action", "press")
        if action not in ("press", "status", "save_state", "load_state"):
            raise ValueError("unknown emulator action")
        if action in ("save_state", "load_state"):
            res, shot = self._get_emu().checkpoint(action, args.get("slot", 0))
        else:
            buttons = args.get("buttons")
            if action == "status":
                buttons = []
            elif buttons is None:
                buttons = [args.get("button", "")]
            res, shot = self._get_emu().input(
                buttons, hold_ms=args.get("hold_ms", 120), gap_ms=args.get("gap_ms", 700))
        labels = {"status": "Inspecting", "press": "Pressing",
                  "save_state": "Saving checkpoint", "load_state": "Loading checkpoint"}
        events.record(self.bot, "emu_input", labels[action],
                      str(res.get("slot")) if "slot" in res else " ".join(res.get("pressed", [])))
        import base64
        return {"content": [{"type": "text", "text": json.dumps(res)},
                            {"type": "image", "data": base64.b64encode(shot).decode(),
                             "mimeType": "image/png"}], "isError": False}

    # -- protocol ---------------------------------------------------------------
    def _tools(self) -> list:
        tools = [_PERCEIVE_TOOL, _GAME_MACRO_TOOL]
        if operator_job_tools.available():
            tools.extend(operator_job_tools.TOOLS)
        if (
            operator_job_tools.available()
            or os.environ.get("OPERATOR_OVERWATCH") == "1"
            or os.environ.get("OPERATOR_RUNTIME") == "agy"
        ):
            tools.append(_OVERWATCH_TOOL)
        if self.surface_name in ("desktop-sandbox", "desktop-real", "auto"):
            tools.append(_COMPUTER_TOOL)
        if self.surface_name in ("desktop-sandbox", "auto"):
            tools.append(_SANDBOX_EXEC_TOOL)
            tools.append(_SANDBOX_WINDOW_TOOL)
            tools.append(_EMU_INPUT_TOOL)   # emulator games (Pokémon Emerald)
        return tools

    @staticmethod
    def _maybe_inject_steer(result: dict) -> dict:
        if (
            operator_steer is not None
            and isinstance(result, dict)
            and isinstance(result.get("content"), list)
        ):
            if os.environ.get("OPERATOR_ANOMALY_INJECT") != "0" and hasattr(operator_steer, "take_anomalies"):
                try:
                    anom = operator_steer.take_anomalies()
                    if anom:
                        result["content"].append({"type": "text", "text": anom})
                except Exception:
                    pass
            if os.environ.get("OPERATOR_STEER_INJECT") == "1":
                try:
                    ctx = operator_steer.consume_live_context()
                    if ctx:
                        result["content"].append({"type": "text", "text": ctx})
                except Exception:
                    pass
        return result

    def handle(self, msg: dict):
        """One JSON-RPC message in → response dict out (None for notifications)."""
        method = msg.get("method", "")
        mid = msg.get("id")
        if mid is None:                       # notification — no response
            return None
        params = msg.get("params") or {}
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion",
                                              PROTOCOL_FALLBACK),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "operator-control", "version": "1.0.0"}}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"tools": self._tools()}}
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if name.startswith('job_'):
                try:
                    result = operator_job_tools.call(name, args)
                except (ValueError, KeyError, PermissionError, OSError) as exc:
                    result = {'isError': True, 'content': [{'type': 'text', 'text': str(exc)}]}
                except Exception:
                    result = {'isError': True, 'content': [{'type': 'text', 'text': 'Job operation failed; inspect the current job before retrying.'}]}
                return {'jsonrpc': '2.0', 'id': mid, 'result': self._maybe_inject_steer(result)}
            handler = {"perceive": self._tool_perceive,
                       "game_macro": self._tool_game_macro,
                       "computer": self._tool_computer,
                       "sandbox_exec": self._handle_sandbox_exec,
                       "sandbox_window": self._handle_sandbox_window,
                       "emu_input": self._tool_emu_input,
                       "overwatch": self._tool_overwatch}.get(name)
            if handler is None:
                result = {"content": [{"type": "text", "text": json.dumps(
                    {"error": f"unknown tool {name!r}"})}], "isError": True}
            else:
                try:
                    result = handler(args)
                except (SurfaceError, SurfaceStopped, maps_mod.MapError,
                        emu_input_mod.EmuInputError, ValueError, OSError) as e:
                    result = {"content": [{"type": "text", "text": json.dumps(
                        {"error": str(e)})}], "isError": True}
                except Exception as e:  # noqa: BLE001 — server must answer, not die
                    result = {"content": [{"type": "text", "text": json.dumps(
                        {"error": f"internal: {e}"})}], "isError": True}
            return {"jsonrpc": "2.0", "id": mid, "result": self._maybe_inject_steer(result)}
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> int:
    srv = OperatorMCP()
    out = sys.stdout
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            resp = srv.handle(msg)
            if resp is not None:
                out.write(json.dumps(resp, ensure_ascii=False) + "\n")
                out.flush()
    finally:
        srv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
