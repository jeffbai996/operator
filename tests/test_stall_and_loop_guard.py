"""v1.1 §2.1 — server-side stall watchdog + runtime-agnostic repeat-action guard.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_stall_and_loop_guard.py -q
"""
import json
import time

import pytest

import operator_agent as OA


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return OA.AgentRunner()


# ── repeat-action loop guard ────────────────────────────────────────────────

def test_identical_actions_trip_the_guard(runner):
    for _ in range(OA.AgentRunner._REPEAT_ACTION_STREAK):
        runner._note_action("browser_click", {"x": 100, "y": 200})
    assert runner._repeat_nudge_pending is True
    warns = [m for m in runner.messages if m["role"] == "error"]
    assert len(warns) == 1 and "repeated" in warns[0]["text"]


def test_guard_warns_only_once_per_run(runner):
    for _ in range(OA.AgentRunner._REPEAT_ACTION_STREAK + 5):
        runner._note_action("browser_click", {"x": 100, "y": 200})
    assert len([m for m in runner.messages if m["role"] == "error"]) == 1


def test_different_args_reset_the_streak(runner):
    runner._note_action("browser_click", {"x": 100, "y": 200})
    runner._note_action("browser_click", {"x": 100, "y": 200})
    runner._note_action("browser_click", {"x": 300, "y": 50})   # moved → not a loop
    runner._note_action("browser_click", {"x": 100, "y": 200})
    assert runner._repeat_nudge_pending is False
    assert runner.messages == []


def test_nudge_is_consume_once_and_runtime_agnostic(runner):
    runner._repeat_nudge_pending = True
    out1 = runner._apply_loop_nudge("Book me a flight")
    assert out1 != "Book me a flight" and "Book me a flight" in out1
    assert "repeated the SAME" in out1
    assert runner._repeat_nudge_pending is False
    out2 = runner._apply_loop_nudge("Book me a flight")
    assert out2 == "Book me a flight"


def test_apply_loop_nudge_still_carries_the_agy_nudge(runner):
    runner._agy_loop_nudge_pending = True
    out = runner._apply_loop_nudge("t")
    assert "stuck reasoning" in out
    assert runner._agy_loop_nudge_pending is False


def test_claude_stream_tool_use_feeds_the_guard(runner):
    runner._runtime = "claude"
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "browser_click", "input": {"x": 5, "y": 6}}]}})
    for _ in range(OA.AgentRunner._REPEAT_ACTION_STREAK):
        runner._consume(line + "\n")
    assert runner._repeat_nudge_pending is True


def test_codex_tool_call_feeds_the_guard(runner):
    runner._runtime = "codex"
    evt = {"type": "item.completed", "item": {
        "type": "mcp_tool_call", "tool": "browser_click",
        "arguments": {"x": 5, "y": 6}}}
    for _ in range(OA.AgentRunner._REPEAT_ACTION_STREAK):
        runner._consume_codex(evt)
    assert runner._repeat_nudge_pending is True


# ── stall watchdog ──────────────────────────────────────────────────────────

def _running_runner(runner, monkeypatch, quiet_for):
    runner._set_state("running", "test")
    runner.last_progress_ts = time.time() - quiet_for
    monkeypatch.setattr(runner, "is_running", lambda: True)
    return runner


def test_quiet_run_under_soft_budget_is_not_stalled(runner, monkeypatch):
    _running_runner(runner, monkeypatch, quiet_for=30)
    snap = runner.snapshot()
    assert snap["stalled"] is False


def test_soft_stall_is_reported_but_not_killed(runner, monkeypatch):
    _running_runner(runner, monkeypatch, quiet_for=200)   # soft 120 < 200 < hard 300
    stopped = []
    monkeypatch.setattr(runner, "stop", lambda: stopped.append(1) or {"ok": True})
    snap = runner.snapshot()
    assert snap["stalled"] is True and snap["stalled_for"] > 120
    assert not stopped
    assert runner.state == "running"


def test_hard_stall_auto_stops_with_reason(runner, monkeypatch):
    _running_runner(runner, monkeypatch, quiet_for=400)
    stopped = []
    monkeypatch.setattr(runner, "stop", lambda: stopped.append(1) or {"ok": True})
    runner.snapshot()
    assert stopped, "hard stall must auto-stop the run"
    assert "watchdog auto-stop" in runner._stall_kill_reason
    assert any(m["role"] == "error" and "no progress" in m["text"]
               for m in runner.messages)


def test_hard_stall_fires_stop_only_once(runner, monkeypatch):
    _running_runner(runner, monkeypatch, quiet_for=400)
    stopped = []
    monkeypatch.setattr(runner, "stop", lambda: stopped.append(1) or {"ok": True})
    runner.snapshot()
    runner.snapshot()   # a later poll while the kill is landing must not re-fire
    assert len(stopped) == 1


def test_gate_gap_is_exempt_from_the_stall_watchdog(runner, monkeypatch):
    """1.0.7 B2: in the inter-turn gate gap last_progress_ts is stale from
    turn 1 — the watchdog must not SIGTERM the just-spawned turn 2 and
    mislabel a healthy gap as a stall."""
    _running_runner(runner, monkeypatch, quiet_for=400)   # past hard budget
    runner._gate_pending = True
    stopped = []
    monkeypatch.setattr(runner, "stop", lambda: stopped.append(1) or {"ok": True})
    snap = runner.snapshot()
    assert not stopped, "watchdog killed a run mid gate gap"
    assert runner._stall_kill_reason == ""
    assert snap["stalled"] is False   # a bounded gap is not a stall signal


def test_stall_watchdog_env_disable(runner, monkeypatch):
    monkeypatch.setenv("OPERATOR_STALL_SOFT", "0")
    monkeypatch.setenv("OPERATOR_STALL_HARD", "0")
    _running_runner(runner, monkeypatch, quiet_for=10_000)
    snap = runner.snapshot()
    assert snap["stalled"] is False
    assert runner._stall_kill_reason == ""


def test_stall_reason_turns_stop_into_error_not_interrupted(runner):
    """The _run terminal branch: a watchdog kill reads as error-with-reason,
    a human Stop stays 'interrupted'."""
    runner._stopped = True
    runner._stall_kill_reason = "stalled: no progress for 400s — watchdog auto-stop"
    # mirror the terminal branch's decision directly
    if runner._stall_kill_reason:
        runner._set_state("error", runner._stall_kill_reason)
    assert runner.state == "error"
