"""macro.py — the local macro controller: slow brain, fast hands.

The planner (LLM) emits a macro — a JSON list of ops — once; this controller
executes it against the active surface at machine speed, re-running perception
between steps and evaluating verify/wait conditions LOCALLY (template counts,
OCR text, pixel change). Zero LLM round-trips mid-macro. It bails back to the
planner on anything unexpected: an unmet verify, an unknown target, a watcher
hit (popup), a budget, or an explicit yield — returning a structured result the
planner reads to re-decide.

Ops:
    {"op": "click_target", "label": str, "index"?: int, "button"?: str,
     "clicks"?: int, "verify"?: COND, "settle_ms"?: int}
    {"op": "click_xy", "x": int, "y": int, ...same extras}
    {"op": "drag_xy", "x1","y1","x2","y2", "duration_ms"?, "verify"?, "settle_ms"?}
    {"op": "type", "text": str}          {"op": "key", "key": str}
    {"op": "scroll", "x","y", "direction"?, "amount"?}
    {"op": "wait_ms", "ms": int}
    {"op": "wait_until", "cond": COND, "timeout_s"?: float, "poll_ms"?: int,
     "on_timeout"?: "stop"|"continue"}
    {"op": "repeat", "ops": [OPS], "until": COND, "max"?: int}
    {"op": "assert", "cond": COND}
    {"op": "yield_to_planner", "reason"?: str}

Conditions (evaluated against the latest WorldState — no LLM):
    {"target": label, "min_count"?: n}     ≥n targets with that label (default 1)
    {"no_target": label}                   zero targets with that label
    {"text": substring}                    substring appears in any OCR hit
    {"count_delta": {"label": l, "cmp": ">="|">"|"=="|"<="|"<", "value": n}}
                                           target count vs the op's PRE-ACTION baseline
    {"pixel_change": {"region": [x,y,w,h], "min_frac"?: 0.05}}
                                           frame region changed vs pre-action frame
    {"all": [CONDS]} / {"any": [CONDS]}

The controller keeps a structured world-model in code (per-label counts, last
WorldState) so the planner reads state instead of re-deriving it from pixels.
"""
from __future__ import annotations

import time

import numpy as np

try:                                   # package import (operator.control.macro)
    from .surfaces import SurfaceError, SurfaceStopped
except ImportError:                    # flat import (isolation tests)
    from surfaces import SurfaceError, SurfaceStopped

_PRIMITIVES = {"click_target", "click_xy", "drag_xy", "type", "key", "scroll",
               "wait_ms"}
_OPS = _PRIMITIVES | {"wait_until", "repeat", "assert", "yield_to_planner"}

_CMPS = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
         "==": lambda a, b: a == b, "<=": lambda a, b: a <= b,
         "<": lambda a, b: a < b}

DEFAULT_SETTLE_MS = 400


# ── validation ───────────────────────────────────────────────────────────────
def _validate_cond(cond, path: str, errs: list) -> None:
    if not isinstance(cond, dict) or not cond:
        errs.append(f"{path}: condition must be a non-empty object")
        return
    known = {"target", "no_target", "text", "count_delta", "pixel_change",
             "min_count", "all", "any"}
    if not set(cond) & known:
        errs.append(f"{path}: unknown condition {sorted(cond)}")
    if "count_delta" in cond:
        cd = cond["count_delta"]
        if not (isinstance(cd, dict) and cd.get("label")
                and cd.get("cmp") in _CMPS and isinstance(cd.get("value"), (int, float))):
            errs.append(f"{path}: count_delta needs label, cmp, value")
    if "pixel_change" in cond:
        pc = cond["pixel_change"]
        if not (isinstance(pc, dict) and isinstance(pc.get("region"), (list, tuple))
                and len(pc["region"]) == 4):
            errs.append(f"{path}: pixel_change needs region [x,y,w,h]")
    for k in ("all", "any"):
        if k in cond:
            if not isinstance(cond[k], list):
                errs.append(f"{path}: {k} must be a list of conditions")
            else:
                for i, c in enumerate(cond[k]):
                    _validate_cond(c, f"{path}.{k}[{i}]", errs)


def validate_macro(ops, _path: str = "ops") -> list:
    """Return a list of error strings; [] means the macro is well-formed."""
    errs: list = []
    if not isinstance(ops, list):
        return [f"{_path}: macro must be a list of ops"]
    for i, op in enumerate(ops):
        p = f"{_path}[{i}]"
        if not isinstance(op, dict):
            errs.append(f"{p}: op must be an object")
            continue
        kind = op.get("op")
        if kind not in _OPS:
            errs.append(f"{p}: unknown op {kind!r} (valid: {sorted(_OPS)})")
            continue
        if kind == "click_target" and not op.get("label"):
            errs.append(f"{p}: click_target needs a label")
        if kind == "click_xy" and not ("x" in op and "y" in op):
            errs.append(f"{p}: click_xy needs x and y")
        if kind == "drag_xy" and not all(k in op for k in ("x1", "y1", "x2", "y2")):
            errs.append(f"{p}: drag_xy needs x1, y1, x2, y2")
        if kind == "type" and "text" not in op:
            errs.append(f"{p}: type needs text")
        if kind == "key" and not op.get("key"):
            errs.append(f"{p}: key needs key")
        if kind == "scroll" and not ("x" in op and "y" in op):
            errs.append(f"{p}: scroll needs x and y")
        if kind == "wait_ms" and not isinstance(op.get("ms"), (int, float)):
            errs.append(f"{p}: wait_ms needs ms")
        if kind == "wait_until":
            if "cond" not in op:
                errs.append(f"{p}: wait_until needs cond")
            else:
                _validate_cond(op["cond"], f"{p}.cond", errs)
        if kind == "assert":
            if "cond" not in op:
                errs.append(f"{p}: assert needs cond")
            else:
                _validate_cond(op["cond"], f"{p}.cond", errs)
        if kind == "repeat":
            if not isinstance(op.get("ops"), list):
                errs.append(f"{p}: repeat needs ops (list)")
            else:
                errs.extend(validate_macro(op["ops"], f"{p}.ops"))
            if "until" in op:
                _validate_cond(op["until"], f"{p}.until", errs)
        if "verify" in op:
            _validate_cond(op["verify"], f"{p}.verify", errs)
    return errs


# ── controller ───────────────────────────────────────────────────────────────
class _Bail(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class MacroController:
    """Executes one macro against a surface. Not thread-safe; one run at a time.

    perceive: callable(frame_ndarray) -> WorldState dict {targets, text, w, h}.
    on_event: optional callable(label: str, detail: str) for live-trace hooks.
    """

    def __init__(self, surface, perceive, on_event=None,
                 step_budget: int = 300, wall_clock_s: float = 240.0) -> None:
        self._surface = surface
        self._perceive = perceive
        self._on_event = on_event
        self._step_budget = step_budget
        self._wall_clock_s = wall_clock_s
        self._steps = 0
        self._deadline = 0.0
        self._ws: dict = {"targets": [], "text": [], "w": 0, "h": 0}
        self._frame = None

    # -- world model ---------------------------------------------------------
    def _refresh(self) -> None:
        self._frame = self._surface.frame()
        self._ws = self._perceive(self._frame)

    def counts(self) -> dict:
        out: dict = {}
        for t in self._ws.get("targets", []):
            out[t.get("label", "")] = out.get(t.get("label", ""), 0) + 1
        return out

    # -- condition evaluation --------------------------------------------------
    def _eval(self, cond: dict, baseline: dict | None = None) -> bool:
        """baseline: {"counts": dict, "frame": ndarray} captured pre-action."""
        if "all" in cond:
            return all(self._eval(c, baseline) for c in cond["all"])
        if "any" in cond:
            return any(self._eval(c, baseline) for c in cond["any"])
        if "target" in cond:
            need = int(cond.get("min_count", 1))
            return self.counts().get(cond["target"], 0) >= need
        if "no_target" in cond:
            return self.counts().get(cond["no_target"], 0) == 0
        if "text" in cond:
            needle = str(cond["text"]).lower()
            return any(needle in str(t.get("text", "")).lower()
                       for t in self._ws.get("text", []))
        if "count_delta" in cond:
            cd = cond["count_delta"]
            before = (baseline or {}).get("counts", {}).get(cd["label"], 0)
            now = self.counts().get(cd["label"], 0)
            return _CMPS[cd["cmp"]](now - before, cd["value"])
        if "pixel_change" in cond:
            pc = cond["pixel_change"]
            base_frame = (baseline or {}).get("frame")
            if base_frame is None or self._frame is None:
                return False
            x, y, w, h = (int(v) for v in pc["region"])
            a = np.asarray(base_frame)[y:y + h, x:x + w].astype(np.int16)
            b = np.asarray(self._frame)[y:y + h, x:x + w].astype(np.int16)
            if a.size == 0 or a.shape != b.shape:
                return False
            changed = (np.abs(a - b).max(axis=-1) > 12).mean()
            return changed >= float(pc.get("min_frac", 0.05))
        return False

    # -- execution -------------------------------------------------------------
    def _emit(self, label: str, detail: str = "") -> None:
        if self._on_event:
            try:
                self._on_event(label, detail)
            except Exception:
                pass

    def _budget(self) -> None:
        self._steps += 1
        if self._steps > self._step_budget:
            self._steps = self._step_budget
            raise _Bail("step_budget")
        if time.monotonic() > self._deadline:
            raise _Bail("wall_clock")

    def _do_primitive(self, op: dict) -> None:
        self._budget()
        kind = op["op"]
        if kind == "click_target":
            self._refresh()
            label = op["label"]
            matches = [t for t in self._ws.get("targets", [])
                       if t.get("label") == label]
            idx = int(op.get("index", 0))
            if idx >= len(matches):
                raise _Bail(f"unknown_target:{label}[{idx}] "
                            f"(visible: {self.counts()})")
            t = matches[idx]
            self._surface.click(int(t["x"]), int(t["y"]),
                                button=op.get("button", "left"),
                                clicks=int(op.get("clicks", 1)))
            self._emit("Clicking", f"{label}[{idx}] ({t['x']}, {t['y']})")
        elif kind == "click_xy":
            self._surface.click(int(op["x"]), int(op["y"]),
                                button=op.get("button", "left"),
                                clicks=int(op.get("clicks", 1)))
            self._emit("Clicking", f"({op['x']}, {op['y']})")
        elif kind == "drag_xy":
            self._surface.drag(int(op["x1"]), int(op["y1"]),
                               int(op["x2"]), int(op["y2"]),
                               duration_ms=int(op.get("duration_ms", 350)))
            self._emit("Dragging",
                       f"({op['x1']}, {op['y1']}) → ({op['x2']}, {op['y2']})")
        elif kind == "type":
            self._surface.type_text(op["text"])
            self._emit("Typing", str(op["text"])[:60])
        elif kind == "key":
            self._surface.key(op["key"])
            self._emit("Pressing", str(op["key"]))
        elif kind == "scroll":
            self._surface.scroll(int(op["x"]), int(op["y"]),
                                 direction=op.get("direction", "down"),
                                 amount=int(op.get("amount", 3)))
            self._emit("Scrolling", op.get("direction", "down"))
        elif kind == "wait_ms":
            time.sleep(float(op["ms"]) / 1000.0)

    def _check_watchers(self, watch: list) -> None:
        if not watch:
            return
        for w in watch:
            if self._eval(w.get("cond", {})):
                raise _Bail(f"watch:{w.get('label', 'watcher')}")

    def _run_ops(self, ops: list, watch: list) -> None:
        for op in ops:
            kind = op.get("op")
            if kind == "yield_to_planner":
                raise _Bail("yield:" + str(op.get("reason", "")))
            if kind == "wait_until":
                self._wait_until(op)
            elif kind == "assert":
                self._budget()
                self._refresh()
                if not self._eval(op["cond"]):
                    raise _Bail(f"assert_failed:{op['cond']}")
            elif kind == "repeat":
                self._repeat(op, watch)
            else:
                baseline = None
                if "verify" in op:
                    self._refresh()
                    baseline = {"counts": self.counts(), "frame": self._frame}
                self._do_primitive(op)
                if "verify" in op:
                    time.sleep(float(op.get("settle_ms", DEFAULT_SETTLE_MS)) / 1000.0)
                    self._refresh()
                    if not self._eval(op["verify"], baseline):
                        raise _Bail(f"verify_failed:{op.get('op')}"
                                    f"@{op.get('label', '')}")
            self._refresh_between_ops(bool(watch))
            self._check_watchers(watch)

    def _refresh_between_ops(self, watching: bool) -> None:
        # Watchers MUST see a fresh view after every op — evaluating them against
        # a WorldState cached before the op means a popup that appears mid-macro
        # never trips the guard. Without watchers, only capture when we have
        # nothing yet (the verify/click_target paths already refreshed).
        if watching or self._frame is None:
            try:
                self._refresh()
            except Exception:
                pass

    def _wait_until(self, op: dict) -> None:
        timeout = float(op.get("timeout_s", 10.0))
        poll = float(op.get("poll_ms", 300)) / 1000.0
        end = time.monotonic() + timeout
        while True:
            self._budget()
            self._refresh()
            if self._eval(op["cond"]):
                return
            if time.monotonic() >= end:
                if op.get("on_timeout") == "continue":
                    return
                raise _Bail(f"wait_timeout:{op['cond']}")
            time.sleep(poll)

    def _repeat(self, op: dict, watch: list) -> None:
        limit = int(op.get("max", 25))
        until = op.get("until")
        for _ in range(limit):
            if until:
                self._refresh()
                if self._eval(until):
                    return
            self._run_ops(op.get("ops", []), watch)
        if until:
            self._refresh()
            if self._eval(until):
                return
            # max iterations without the goal — the planner must re-decide
            raise _Bail(f"repeat_max:{limit}")

    def run(self, ops: list, watch: list | None = None) -> dict:
        """Execute the macro. Always returns a result dict, never raises."""
        started = time.monotonic()
        self._deadline = started + self._wall_clock_s
        self._steps = 0
        errs = validate_macro(ops)
        if errs:
            return self._result(False, "invalid_macro: " + "; ".join(errs[:5]),
                                started)
        done, reason = True, None
        try:
            self._run_ops(ops, watch or [])
        except _Bail as b:
            done, reason = False, b.reason
        except SurfaceStopped:
            done, reason = False, "stopped"
        except SurfaceError as e:
            done, reason = False, f"surface_error: {e}"
        except Exception as e:  # noqa: BLE001 — controller must return, not raise
            done, reason = False, f"error: {e}"
        return self._result(done, reason, started)

    def _result(self, done: bool, reason, started: float) -> dict:
        return {"done": done,
                "steps_executed": self._steps,
                "stopped_reason": reason,
                "world_state": self._ws,
                "counts": self.counts(),
                "elapsed_s": round(time.monotonic() - started, 2)}
