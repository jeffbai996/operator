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


def test_manual_takeover_waits_for_active_tool_and_arms_timeout(runner, monkeypatch):
    timers = []

    class FakeTimer:
        def __init__(self, delay, fn):
            timers.append((delay, fn))
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(OA.threading, "Timer", FakeTimer)
    monkeypatch.setattr(runner, "is_running", lambda: True)
    runner._runtime = "codex"
    runner._tool_active = True

    result = runner.request_takeover(timeout_s=12)

    assert result == {"ok": True, "pending": True, "timeout_s": 12.0}
    assert runner._takeover_requested is True
    assert timers and timers[0][0] == 12


def test_manual_takeover_timeout_stops_a_stuck_tool(runner, monkeypatch):
    callbacks = []
    stops = []

    class FakeTimer:
        def __init__(self, _delay, fn):
            callbacks.append(fn)
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(OA.threading, "Timer", FakeTimer)
    monkeypatch.setattr(runner, "is_running", lambda: True)
    monkeypatch.setattr(runner, "stop", lambda: stops.append("stop") or {"ok": True})
    runner._runtime = "codex"
    runner._tool_active = True

    runner.request_takeover(timeout_s=12)
    callbacks[0]()

    assert stops == ["stop"]
    assert runner._takeover_requested is False


def test_gemini_manual_takeover_uses_shorter_fallback(runner, monkeypatch):
    """Agy has no structured tool-result seam, so its fallback is the whole
    user-visible wait. Keep MAN responsive instead of inheriting the longer
    structured-runtime safety window."""
    timers = []

    class FakeTimer:
        def __init__(self, delay, _fn):
            timers.append(delay)
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(OA.threading, "Timer", FakeTimer)
    monkeypatch.setattr(runner, "is_running", lambda: True)
    runner._runtime = "agy"
    runner._tool_active = None

    result = runner.request_takeover()

    assert result == {"ok": True, "pending": True, "timeout_s": 4.0}
    assert timers == [4.0]


def test_run_inner_never_resets_the_cancel_flag():
    """Grep-level guard: _run_inner's per-run reset block must never WRITE
    _cancel_requested — that wipe was the whole B3 bug class. Reading it is
    fine (the post-spawn check that honors a pre-spawn Stop does exactly
    that); only start() may clear it."""
    import inspect, re
    src = inspect.getsource(OA.AgentRunner._run_inner)
    assert not re.search(r"_cancel_requested\s*=(?!=)", src)


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


def test_stop_kills_group_after_leader_exits_but_descendant_holds_stdout(
        runner, monkeypatch):
    """The CLI leader can exit before a child that inherited its stdout pipe.
    Stop must address the retained process-group id, not ask the dead leader
    for its group and leave the reader thread plus admission slot wedged."""
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kw):
        return real_popen(["bash", "-c", "sleep 30 & exit 0"],
                          stdout=subprocess.PIPE, text=True,
                          start_new_session=True)

    monkeypatch.setattr(OA.subprocess, "Popen", fake_popen)
    assert runner.start("gemma", "leader exits first")["ok"]
    proc = None
    try:
        for _ in range(100):
            proc = runner._proc
            if proc is not None and proc.poll() is not None:
                break
            time.sleep(0.02)
        assert proc is not None and proc.poll() is not None
        assert runner._thread.is_alive(), "fixture did not reproduce blocked stdout"

        assert runner.stop()["ok"]
        runner._thread.join(timeout=3)
        assert not runner._thread.is_alive(), "dead leader left runner thread wedged"
        assert runner.state == "interrupted"
        assert not runner.is_running()
    finally:
        if proc is not None:
            try:
                import os
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# ── 2026-07-12: `alive` must not lie at birth or wrap-up ─────────────────────
# The client's dead-run watchdog kills any run whose poll reads alive:false
# past its grace window. is_running() returning False in the PRE-SPAWN window
# (run thread building the command, no proc yet) reported newborn runs as
# dead — combined with the client's stale watchdog anchor, that killed first
# turns as a bare "Error" (ledger run #10, 2026-07-11). Thread-aliveness is
# the truth: _run always lands a terminal state before its thread exits.

def _held_run(runner):
    """Replace _run with a body that parks the thread in 'pre-spawn' (no proc
    ever assigned) until released. Returns the release event."""
    gate = threading.Event()
    runner._run = lambda binpath, b, task: gate.wait(5)
    return gate


def test_is_running_true_during_pre_spawn_window(runner):
    gate = _held_run(runner)
    assert runner.start("claude-a", "t")["ok"]
    try:
        assert runner._proc is None
        assert runner.is_running() is True
        assert runner.snapshot()["alive"] is True
    finally:
        gate.set()


def test_second_dispatch_rejected_during_pre_spawn(runner):
    gate = _held_run(runner)
    assert runner.start("claude-a", "t")["ok"]
    try:
        res = runner.start("claude-a", "t2")
        assert not res["ok"] and "already running" in res["error"]
    finally:
        gate.set()


def test_stop_during_pre_spawn_cancels_instead_of_unwedging(runner):
    """A Stop with no proc but a LIVE run thread is a pre-spawn cancel, not a
    stale-state unwedge — flipping to idle here orphaned the imminent spawn."""
    gate = _held_run(runner)
    assert runner.start("claude-a", "t")["ok"]
    try:
        res = runner.stop()
        assert res["ok"] and res.get("cancelled_prespawn")
        assert runner.state == "running"   # the run thread still owns the terminal state
        assert runner._cancel_requested and runner._stopped
    finally:
        gate.set()


def test_is_running_true_during_wrap_up_sliver(runner):
    """Process exited (poll() != None) but the run thread is still landing the
    terminal state — that's the tail of a live run, not a death."""
    class _DoneProc:
        def poll(self):
            return 0
    gate = _held_run(runner)
    assert runner.start("claude-a", "t")["ok"]
    runner._proc = _DoneProc()
    try:
        assert runner.is_running() is True
    finally:
        gate.set()
        runner._proc = None


def test_wedged_dead_thread_still_reads_not_running(runner):
    """The one genuine wedge — state stuck 'running', run thread gone — must
    still read as not-running so the unwedge paths (stop/reset) keep working."""
    runner.state = "running"
    runner._proc = None
    assert runner.is_running() is False


# ── agy model/effort contract (2026-07-24) ──────────────────────────────────
# agy used to take ONE folded display string ("Gemini 3.5 Flash (High)") and had
# no effort flag. It now exposes --effort and REJECTS the folded form alongside
# it ("--effort is not supported for model …"), so start() must pass a bare slug
# + a separate effort, and must NOT send effort for a slug whose tier is baked in.

def _agy_start(runner, monkeypatch, **kw):
    """Run start() for the agy-runtime bot without spawning a thread."""
    monkeypatch.setattr(OA.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    monkeypatch.setitem(OA.AGENT_BOTS, "gemma", {**OA.AGENT_BOTS["gemma"], "runtime": "agy"})
    runner.start("gemma", "do a thing", **kw)
    return runner.model, runner.effort


def test_agy_bare_slug_keeps_separate_effort(runner, monkeypatch):
    model, effort = _agy_start(runner, monkeypatch, model="gemini-3.7-flash", effort="medium")
    assert model == "gemini-3.7-flash"   # no "(Medium)" folded in
    assert effort == "medium"            # rides its own --effort flag


def test_agy_baked_tier_slug_drops_effort(runner, monkeypatch):
    # tier already in the slug → sending --effort too would 400 in agy
    model, effort = _agy_start(runner, monkeypatch, model="gemini-3.7-flash-low", effort="high")
    assert model == "gemini-3.7-flash-low"
    assert effort == ""


def test_agy_display_name_entry_drops_effort(runner, monkeypatch):
    # Claude/GPT-OSS entries keep the parenthesised display form — `agy models`
    # prints "claude-sonnet-4-6-thinking" but --model rejects that slug.
    model, effort = _agy_start(runner, monkeypatch,
                               model="Claude Sonnet 4.6 (Thinking)", effort="high")
    assert model == "Claude Sonnet 4.6 (Thinking)"
    assert effort == ""


def test_agy_defaults_to_current_flash_slug(runner, monkeypatch):
    model, effort = _agy_start(runner, monkeypatch, model="", effort="")
    assert model == "gemini-3.7-flash"
    assert effort == "high"
