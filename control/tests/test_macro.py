"""Tests for macro.py — the planner/controller split's fast hands.

All pure: a FakeSurface records injected actions and serves synthetic frames;
perception is a scripted function. No browser, no display, no LLM.
"""
import numpy as np
import pytest

import macro as M


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeSurface:
    name = "fake"

    def __init__(self, frames=None):
        self.calls = []
        self._frames = list(frames or [])
        self._last = self._frames[-1] if self._frames else np.zeros((100, 100, 3),
                                                                    dtype=np.uint8)

    def frame(self):
        if self._frames:
            self._last = self._frames.pop(0)
        return self._last

    def click(self, x, y, button="left", clicks=1):
        self.calls.append(("click", x, y, button, clicks))

    def drag(self, x1, y1, x2, y2, duration_ms=350):
        self.calls.append(("drag", x1, y1, x2, y2))

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def type_text(self, text):
        self.calls.append(("type", text))

    def key(self, combo):
        self.calls.append(("key", combo))

    def scroll(self, x, y, direction="down", amount=3):
        self.calls.append(("scroll", x, y, direction, amount))


def ws(targets=None, text=None):
    return {"targets": targets or [], "text": text or [], "w": 100, "h": 100}


def tgt(label, x=10, y=10, score=0.9):
    return {"label": label, "x": x, "y": y, "score": score}


def scripted_perceive(states):
    """perceive(frame) that returns each state in turn, then repeats the last."""
    seq = list(states)

    def _p(frame):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return _p


def controller(surface=None, states=None, **kw):
    return M.MacroController(surface or FakeSurface(),
                             perceive=scripted_perceive(states or [ws()]), **kw)


# ── validation ───────────────────────────────────────────────────────────────
def test_validate_rejects_unknown_op():
    errs = M.validate_macro([{"op": "explode"}])
    assert errs and "explode" in errs[0]


def test_validate_rejects_missing_fields():
    assert M.validate_macro([{"op": "click_xy"}])            # no x/y
    assert M.validate_macro([{"op": "click_target"}])        # no label
    assert M.validate_macro([{"op": "wait_until"}])          # no cond
    assert M.validate_macro([{"op": "repeat", "ops": []}]) == []  # empty body ok
    assert M.validate_macro([{"op": "repeat"}])              # no ops


def test_validate_accepts_wellformed_macro():
    ops = [
        {"op": "click_target", "label": "tree", "index": 1,
         "verify": {"pixel_change": {"region": [0, 0, 50, 50]}}},
        {"op": "wait_until", "cond": {"target": "log"}, "timeout_s": 5},
        {"op": "repeat", "ops": [{"op": "click_xy", "x": 5, "y": 5}],
         "until": {"target": "full"}, "max": 3},
        {"op": "yield_to_planner", "reason": "banked"},
    ]
    assert M.validate_macro(ops) == []


def test_run_rejects_invalid_macro_without_executing():
    s = FakeSurface()
    r = controller(surface=s).run([{"op": "nope"}])
    assert not r["done"] and r["stopped_reason"].startswith("invalid_macro")
    assert s.calls == []


# ── execution basics ─────────────────────────────────────────────────────────
def test_simple_sequence_executes_all_ops():
    s = FakeSurface()
    r = controller(surface=s).run([
        {"op": "click_xy", "x": 40, "y": 50},
        {"op": "type", "text": "hi"},
        {"op": "key", "key": "enter"},
        {"op": "wait_ms", "ms": 1},
    ])
    assert r["done"] is True and r["stopped_reason"] is None
    assert ("click", 40, 50, "left", 1) in s.calls
    assert ("type", "hi") in s.calls and ("key", "enter") in s.calls
    assert r["steps_executed"] == 4


def test_click_target_resolves_from_perception():
    s = FakeSurface()
    st = ws(targets=[tgt("tree", 30, 40, 0.99), tgt("tree", 60, 70, 0.8)])
    r = controller(surface=s, states=[st]).run(
        [{"op": "click_target", "label": "tree", "index": 1}])
    assert r["done"]
    assert ("click", 60, 70, "left", 1) in s.calls


def test_click_target_unknown_label_stops():
    s = FakeSurface()
    r = controller(surface=s, states=[ws()]).run(
        [{"op": "click_target", "label": "ghost"},
         {"op": "type", "text": "never"}])
    assert not r["done"]
    assert r["stopped_reason"].startswith("unknown_target")
    assert ("type", "never") not in s.calls


# ── verify ───────────────────────────────────────────────────────────────────
def test_verify_pass_continues_and_fail_stops():
    # verify: after clicking, a "log" target must appear. First macro sees it
    # appear (pass); second doesn't (fail).
    ok_states = [ws(targets=[tgt("tree")]), ws(targets=[tgt("tree"), tgt("log")])]
    r = controller(states=ok_states).run(
        [{"op": "click_target", "label": "tree", "settle_ms": 0,
          "verify": {"target": "log"}}])
    assert r["done"]

    bad_states = [ws(targets=[tgt("tree")]), ws(targets=[tgt("tree")])]
    r2 = controller(states=bad_states).run(
        [{"op": "click_target", "label": "tree", "settle_ms": 0,
          "verify": {"target": "log"}}])
    assert not r2["done"] and r2["stopped_reason"].startswith("verify_failed")


def test_verify_count_delta_uses_pre_action_baseline():
    # 2 logs before the click, 3 after → delta >= 1 passes
    states = [ws(targets=[tgt("log"), tgt("log"), tgt("tree")]),
              ws(targets=[tgt("log"), tgt("log"), tgt("log"), tgt("tree")])]
    r = controller(states=states).run(
        [{"op": "click_target", "label": "tree", "settle_ms": 0,
          "verify": {"count_delta": {"label": "log", "cmp": ">=", "value": 1}}}])
    assert r["done"], r["stopped_reason"]


# ── wait_until ───────────────────────────────────────────────────────────────
def test_wait_until_polls_to_success():
    states = [ws(), ws(), ws(targets=[tgt("bank_open")])]
    r = controller(states=states).run(
        [{"op": "wait_until", "cond": {"target": "bank_open"},
          "timeout_s": 2, "poll_ms": 1}])
    assert r["done"]


def test_wait_until_times_out_and_stops():
    r = controller(states=[ws()]).run(
        [{"op": "wait_until", "cond": {"target": "never"},
          "timeout_s": 0.05, "poll_ms": 10}])
    assert not r["done"] and r["stopped_reason"].startswith("wait_timeout")


def test_wait_until_on_timeout_continue():
    r = controller(states=[ws()]).run(
        [{"op": "wait_until", "cond": {"target": "never"},
          "timeout_s": 0.05, "poll_ms": 10, "on_timeout": "continue"},
         {"op": "type", "text": "went on"}])
    assert r["done"]


# ── repeat ───────────────────────────────────────────────────────────────────
def test_repeat_until_cond_met():
    # inventory fills after two iterations (the "full" marker appears)
    states = [ws(), ws(), ws(), ws(targets=[tgt("inventory_full")])]
    s = FakeSurface()
    r = M.MacroController(s, perceive=scripted_perceive(states)).run(
        [{"op": "repeat", "ops": [{"op": "click_xy", "x": 1, "y": 2}],
          "until": {"target": "inventory_full"}, "max": 10}])
    assert r["done"]
    clicks = [c for c in s.calls if c[0] == "click"]
    assert 1 <= len(clicks) < 10


def test_repeat_respects_max():
    s = FakeSurface()
    r = M.MacroController(s, perceive=scripted_perceive([ws()])).run(
        [{"op": "repeat", "ops": [{"op": "click_xy", "x": 1, "y": 2}],
          "until": {"target": "never"}, "max": 3}])
    # hitting max is NOT completion — the planner must re-decide
    assert not r["done"] and r["stopped_reason"].startswith("repeat_max")
    assert len([c for c in s.calls if c[0] == "click"]) == 3


# ── stops, budgets, watchers ─────────────────────────────────────────────────
def test_yield_to_planner_stops_with_reason():
    r = controller().run([{"op": "yield_to_planner", "reason": "banked"}])
    assert not r["done"] and r["stopped_reason"] == "yield:banked"


def test_step_budget_enforced():
    s = FakeSurface()
    ops = [{"op": "click_xy", "x": 1, "y": 1}] * 10
    r = M.MacroController(s, perceive=scripted_perceive([ws()]),
                          step_budget=4).run(ops)
    assert not r["done"] and r["stopped_reason"] == "step_budget"
    assert len(s.calls) == 4


def test_watcher_bails_between_ops():
    # a popup appears after the first op → watcher stops the macro
    states = [ws(), ws(targets=[tgt("popup")]), ws(targets=[tgt("popup")])]
    r = M.MacroController(FakeSurface(), perceive=scripted_perceive(states)).run(
        [{"op": "click_xy", "x": 1, "y": 1},
         {"op": "click_xy", "x": 2, "y": 2},
         {"op": "click_xy", "x": 3, "y": 3}],
        watch=[{"label": "popup_guard", "cond": {"target": "popup"}}])
    assert not r["done"] and r["stopped_reason"] == "watch:popup_guard"


def test_pixel_change_cond():
    dark = np.zeros((100, 100, 3), dtype=np.uint8)
    bright = np.full((100, 100, 3), 255, dtype=np.uint8)
    s = FakeSurface(frames=[dark, bright, bright])
    r = M.MacroController(s, perceive=lambda f: ws()).run(
        [{"op": "click_xy", "x": 1, "y": 1, "settle_ms": 0,
          "verify": {"pixel_change": {"region": [0, 0, 50, 50],
                                      "min_frac": 0.05}}}])
    assert r["done"], r["stopped_reason"]


def test_surface_stop_maps_to_stopped_reason():
    class StoppingSurface(FakeSurface):
        def click(self, *a, **kw):
            raise M.SurfaceStopped("STOP engaged")
    r = M.MacroController(StoppingSurface(),
                          perceive=scripted_perceive([ws()])).run(
        [{"op": "click_xy", "x": 1, "y": 1}])
    assert not r["done"] and r["stopped_reason"] == "stopped"


def test_result_carries_world_state_and_counts():
    states = [ws(targets=[tgt("log"), tgt("log")])]
    r = controller(states=states).run([{"op": "click_target", "label": "log"}])
    assert r["world_state"]["targets"]
    assert r["counts"] == {"log": 2}
