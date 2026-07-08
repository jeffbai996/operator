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
import sys
import time

import numpy as np
from PIL import Image

import events
import maps as maps_mod
import overlay
import perceive as perceive_mod
from macro import MacroController, validate_macro  # noqa: F401 (validate re-exported)
from surfaces import SurfaceError, SurfaceStopped, get_surface

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
        "return_image=true also returns that image inline."),
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
        "condition) and read the structured result; it bails back to you on "
        "unmet verify, unknown target, watcher hit, budget, or yield. Ops: "
        "click_target{label,index?,verify?} click_xy{x,y} drag_xy{x1,y1,x2,y2} "
        "type{text} key{key} scroll{x,y,direction?} wait_ms{ms} "
        "wait_until{cond,timeout_s?,on_timeout?} repeat{ops,until,max?} "
        "assert{cond} yield_to_planner{reason}. "
        "COORDINATES: all xy coords and cond regions are in PERCEIVE frame "
        "space — derive them from perceive (its grid/targets/crop), NOT from "
        "browser_take_screenshot (different scale → misclicks). Prefer "
        "click_target by label over raw click_xy whenever perceive shows the "
        "target. Keep verify regions tight around the EXPECTED change (one "
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
        "left_click_drag / type / key / scroll / wait."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "coordinate": {"type": "array", "items": {"type": "integer"}},
            "start_coordinate": {"type": "array", "items": {"type": "integer"}},
            "text": {"type": "string"},
            "duration": {"type": "number"},
            "scroll_direction": {"type": "string"},
            "scroll_amount": {"type": "integer"},
        },
        "required": ["action"],
    },
}


def _cap_ws(ws: dict) -> dict:
    """Bound the WorldState lists so a busy frame can't flood the context."""
    out = dict(ws)
    out["targets"] = list(ws.get("targets", []))[:_MAX_TARGETS]
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

    # -- surface / perception ------------------------------------------------
    def _get_surface(self):
        if self._surface is None:
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
        if args.get("region"):
            img = np.asarray(overlay.crop_region(frame, args["region"]))
        elif args.get("grid"):
            img = np.asarray(overlay.draw_grid(frame))
        elif args.get("annotate"):
            img = np.asarray(overlay.annotate_targets(frame, ws.get("targets", [])))
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
        return {"content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False)}],
                "isError": False}

    def _tool_computer(self, args: dict) -> dict:
        if self.surface_name == "browser":
            raise SurfaceError(
                "the computer tool is for desktop surfaces — on the browser "
                "surface use your Playwright browser tools")
        surface = self._get_surface()
        action = args.get("action", "")
        coord = args.get("coordinate") or [0, 0]
        label, detail = "Acting", action
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
            start = args.get("start_coordinate") or [0, 0]
            surface.drag(int(start[0]), int(start[1]),
                         int(coord[0]), int(coord[1]))
            label, detail = "Dragging", f"({start[0]}, {start[1]}) → ({coord[0]}, {coord[1]})"
        elif action == "type":
            surface.type_text(args.get("text", ""))
            label, detail = "Typing", str(args.get("text", ""))[:60]
        elif action == "key":
            surface.key(args.get("text", ""))
            label, detail = "Pressing", str(args.get("text", ""))
        elif action == "scroll":
            surface.scroll(int(coord[0]), int(coord[1]),
                           direction=args.get("scroll_direction", "down"),
                           amount=int(args.get("scroll_amount", 3)))
            label = "Scrolling"
        elif action == "wait":
            time.sleep(min(float(args.get("duration", 1.0)), 10.0))
            label = "Waiting"
        else:
            raise SurfaceError(f"unsupported computer action: {action!r}")
        events.record(self.bot, "computer", label, detail)
        return {"content": [{"type": "text", "text": json.dumps({"ok": True})}],
                "isError": False}

    # -- protocol ---------------------------------------------------------------
    def _tools(self) -> list:
        tools = [_PERCEIVE_TOOL, _GAME_MACRO_TOOL]
        if self.surface_name.startswith("desktop"):
            tools.append(_COMPUTER_TOOL)
        return tools

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
            handler = {"perceive": self._tool_perceive,
                       "game_macro": self._tool_game_macro,
                       "computer": self._tool_computer}.get(name)
            if handler is None:
                result = {"content": [{"type": "text", "text": json.dumps(
                    {"error": f"unknown tool {name!r}"})}], "isError": True}
            else:
                try:
                    result = handler(args)
                except (SurfaceError, SurfaceStopped, maps_mod.MapError,
                        ValueError, OSError) as e:
                    result = {"content": [{"type": "text", "text": json.dumps(
                        {"error": str(e)})}], "isError": True}
                except Exception as e:  # noqa: BLE001 — server must answer, not die
                    result = {"content": [{"type": "text", "text": json.dumps(
                        {"error": f"internal: {e}"})}], "isError": True}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> int:
    srv = OperatorMCP()
    out = sys.stdout
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
