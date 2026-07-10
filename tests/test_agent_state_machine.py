"""v1.1 §2.2/§2.3 — the state machine has one writer and stop always lands.

Covers the wedge classes the owner actually hit: a pre-spawn exception leaving a
phantom state='running' with no thread, and Stop answering "nothing running"
while the UI still showed a run (stale running, dead process).

Run from modules/operator:  PYTHONPATH=. pytest tests/test_agent_state_machine.py -q
"""
import json
import subprocess
import threading
import time

import pytest

import operator_agent as OA


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(OA, "_resolve_claude", lambda: "/fake/claude")
    monkeypatch.setattr(OA, "_resolve_codex", lambda: "/fake/codex")
    monkeypatch.setattr(OA, "_resolve_agy", lambda: "/fake/agy")
    return OA.AgentRunner()


# ── §2.2: pre-spawn exception must revert, not leave a phantom 'running' ────

def test_pre_spawn_exception_reverts_to_error(runner, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("thread pool exploded")
    monkeypatch.setattr(OA.threading, "Thread", boom)
    res = runner.start("claude-a", "t")
    assert not res["ok"] and "launch failed" in res["error"]
    assert runner.state == "error"
    assert any(m["role"] == "error" for m in runner.messages)


def test_run_dispatchable_after_pre_spawn_failure(runner, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("boom")
    real_thread = OA.threading.Thread
    monkeypatch.setattr(OA.threading, "Thread", boom)
    assert not runner.start("claude-a", "t")["ok"]
    # the failure must not wedge the runner: the next dispatch goes through
    monkeypatch.setattr(OA.threading, "Thread", real_thread)
    runner._run = lambda binpath, b, task: None
    assert runner.start("claude-a", "t2")["ok"]


def test_set_state_stamps_progress_heartbeat(runner):
    assert runner.last_progress_ts == 0.0
    runner._set_state("running", "test")
    assert runner.last_progress_ts > 0
    t0 = runner.last_progress_ts
    time.sleep(0.01)
    runner._touch()
    assert runner.last_progress_ts > t0


def test_state_never_written_outside_chokepoint():
    """Grep-level guard: `self.state =` must appear ONLY in _set_state (and the
    __init__ default). A new bare write reopens the phantom-state class."""
    import inspect
    import re
    src = inspect.getsource(OA)
    bare = [ln for ln in src.splitlines()
            if re.search(r"self\.state(\s*:\s*\w+)?\s*=[^=]", ln)]
    # exactly two legal sites: the __init__ default and the chokepoint body
    assert len(bare) == 2, bare


# ── §2.3: stop always lands ─────────────────────────────────────────────────

def test_stop_unwedges_stale_running(runner):
    runner.state = "running"      # simulate: _run finally never landed
    runner._proc = None
    res = runner.stop()
    assert res["ok"] and res.get("unwedged")
    assert runner.state == "idle"
    # and the runner is dispatchable again
    runner._run = lambda binpath, b, task: None
    assert runner.start("claude-a", "t")["ok"]


def test_stop_with_nothing_running_is_still_a_noop(runner):
    res = runner.stop()
    assert not res["ok"] and "nothing running" in res["error"]
    assert runner.state == "idle"


def test_snapshot_survives_concurrent_consume_appends(runner):
    """1.0.7 B1: the Flask poll thread reads messages while the run thread
    appends. snapshot() must iterate a copy taken under the lock — a torn
    read here 500s /operator/status under any active run."""
    runner._runtime = "claude"
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "step"}]}}) + "\n"
    stop_evt = threading.Event()
    writer_errors: list = []

    def writer():
        try:
            # bounded: enough churn to interleave with the poll loop, small
            # enough that snapshot's copy+serialize stays test-speed
            for _ in range(4000):
                if stop_evt.is_set():
                    return
                runner._consume(line)
        except Exception as e:  # noqa: BLE001 — the test asserts nothing escapes
            writer_errors.append(e)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        while t.is_alive():
            snap = runner.snapshot()
            # serialize like the Flask layer does — this is where a mid-append
            # torn structure would blow up
            json.dumps(snap["messages"])
            json.dumps(snap["final"])
    finally:
        stop_evt.set()
        t.join(timeout=10)
    assert not writer_errors
    assert any(m["role"] == "assistant" for m in runner.messages)


# ── 1.0.7 B3: Stop during the gate gap cancels turn 2 ───────────────────────

def test_stop_during_gate_gap_cancels_turn_two(runner, monkeypatch):
    """A Stop in the inter-turn gap used to be wiped by turn 2's per-run
    reset of _stopped; the dispatch must gate on _cancel_requested instead."""
    calls: list = []

    def fake_inner(binpath, b, task):
        calls.append(task)
        runner._gate_pending = True   # turn 1 armed the gate...
        runner.stop()                 # ...and the user stops mid-gap
        return "GATE PROMPT"

    monkeypatch.setattr(runner, "_run_inner", fake_inner)
    runner._set_state("running", "test")
    runner._run("/fake/claude", {"runtime": "claude"}, "t")
    assert calls == ["t"], "second _run_inner spawned despite the stop"
    assert runner.state == "interrupted"
    assert runner._gate_pending is False


def test_stop_in_gate_gap_reports_ok_without_unwedging(runner):
    """In the gap there is no proc to kill, but the run is legitimately alive —
    stop() must arm the cancel flag and NOT force state to idle (the run
    thread lands the terminal state itself)."""
    runner._set_state("running", "gate gap")
    runner._proc = None
    runner._gate_pending = True
    res = runner.stop()
    assert res["ok"]
    assert runner._cancel_requested is True
    assert runner.state == "running"


def test_start_clears_cancel_requested(runner):
    runner._cancel_requested = True
    runner._run = lambda binpath, b, task: None
    assert runner.start("claude-a", "t")["ok"]
    assert runner._cancel_requested is False


def test_run_inner_never_resets_the_cancel_flag():
    """Grep-level guard: _run_inner's per-run reset block must not touch
    _cancel_requested — that wipe was the whole B3 bug class."""
    import inspect
    src = inspect.getsource(OA.AgentRunner._run_inner)
    assert "_cancel_requested" not in src


# ── 1.0.7 B4: terminal-reason priority is deterministic under races ─────────

def test_token_cap_stop_beats_stall_label(runner):
    runner._tok_stop_fired = True
    runner._stopped = True
    runner._stall_kill_reason = "stalled: no progress for 400s — watchdog auto-stop"
    state, reason = runner._resolve_terminal(-15)
    assert state == "interrupted" and "token cap" in reason


def test_stall_kill_resolves_to_error_with_reason(runner):
    runner._stopped = True
    runner._stall_kill_reason = "stalled: no progress for 400s — watchdog auto-stop"
    state, reason = runner._resolve_terminal(-15)
    assert state == "error" and "watchdog" in reason


def test_token_cap_racing_clean_exit_is_not_done(runner):
    runner._tok_stop_fired = True
    state, _ = runner._resolve_terminal(0)
    assert state == "interrupted"


def test_user_stop_racing_clean_exit_is_interrupted(runner):
    runner._stopped = True
    state, _ = runner._resolve_terminal(0)
    assert state == "interrupted"


def test_unraced_terminal_labels_are_unchanged(runner):
    assert runner._resolve_terminal(0) == ("done", "exit 0")
    assert runner._resolve_terminal(2) == ("error", "exit 2")
    runner._stopped = True
    assert runner._resolve_terminal(-15) == ("interrupted", "user stop")


def test_stop_kills_real_run_and_leaves_no_orphans(runner, monkeypatch):
    """Start a fake run whose subprocess blocks forever; stop() must kill the
    process group, let _run land its finally, and leave a clean dispatchable
    runner: no live proc, no phantom state."""
    real_popen = subprocess.Popen   # capture BEFORE patching (OA.subprocess IS subprocess)
    def fake_popen(cmd, **kw):
        return real_popen(["sleep", "30"], stdout=subprocess.PIPE,
                          text=True, start_new_session=True)
    monkeypatch.setattr(OA.subprocess, "Popen", fake_popen)
    assert runner.start("claude-a", "block forever")["ok"]
    # wait until the run thread actually has the proc up
    for _ in range(100):
        if runner._proc is not None:
            break
        time.sleep(0.05)
    assert runner.is_running()
    proc = runner._proc
    assert runner.stop()["ok"]
    runner._thread.join(timeout=10)
    assert not runner._thread.is_alive(), "run thread orphaned after stop"
    assert proc.poll() is not None, "subprocess survived stop()"
    assert runner._proc is None
    assert runner.state == "interrupted"     # user stop, not error
    assert not runner.is_running()
    # dispatchable again
    monkeypatch.setattr(OA.subprocess, "Popen",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError))
    runner._run = lambda binpath, b, task: None
    assert runner.start("claude-a", "next")["ok"]
