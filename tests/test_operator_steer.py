"""Mid-run steering (1.0.12) — talk to a live run without killing it.

Before this, a message sent mid-run STOPPED the agent and re-dispatched
(interrupt-steer). Now a steer reaches the live agent through two seams:
  * mid-loop (claude runtime): steer_hook.py, a PostToolUse hook in the bot's
    operator cwd, consumes the steer queue and injects it as additionalContext
    right after the agent's next tool call (verified live on CLI 2.1.207;
    project-level .claude/settings.json is resume-safe — the --settings FLAG
    is the thing that breaks --resume, not the file),
  * exit-seam (every runtime): steers still queued when the process exits
    cleanly trigger one more resumed turn, same machinery as the §3.3 gate.

The queue is cross-PROCESS (the hook runs inside the spawned agent, not the
server), so the store uses O_APPEND pushes + rename-claim consumption instead
of an in-process lock.
"""
import importlib
import json
import os
import subprocess
import sys
import types

import pytest

import operator_steer as OSTEER

_MOD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STEER_PATH", str(tmp_path / "steer.ndjson"))
    return importlib.reload(OSTEER)


# ── the queue store ─────────────────────────────────────────────────────────

def test_push_pending_take_roundtrip(store):
    assert store.pending() == []
    store.push("check the price in CAD")
    store.push("actually use the mobile site")
    got = store.pending()
    assert [s["text"] for s in got] == ["check the price in CAD",
                                       "actually use the mobile site"]
    taken = store.take_all()
    assert [s["text"] for s in taken] == [s["text"] for s in got]
    assert store.pending() == []          # consumed
    assert store.take_all() == []         # idempotent on empty


def test_push_validates(store):
    with pytest.raises(ValueError):
        store.push("")                    # empty
    with pytest.raises(ValueError):
        store.push("x" * (store.MAX_TEXT + 1))   # oversize
    for i in range(store.MAX_PENDING):
        store.push(f"steer {i}")
    with pytest.raises(ValueError):
        store.push("one too many")        # queue cap


def test_clear_and_corrupt_tolerance(store):
    store.push("a")
    store.clear()
    assert store.pending() == []
    # garbage lines are skipped, valid lines survive
    with open(store.path(), "a", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps({"ts": 1.0, "text": "valid"}) + "\n")
    assert [s["text"] for s in store.pending()] == ["valid"]


# ── the PostToolUse hook script ──────────────────────────────────────────────

def _run_hook(env_path: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, OPERATOR_STEER_PATH=env_path)
    return subprocess.run(
        [sys.executable, os.path.join(_MOD_DIR, "steer_hook.py")],
        capture_output=True, text=True, timeout=10, env=env)


def test_hook_consumes_and_emits_context(store):
    store.push("switch to the CAD listing")
    r = _run_hook(store.path())
    assert r.returncode == 0
    out = json.loads(r.stdout)
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PostToolUse"
    assert "switch to the CAD listing" in ctx["additionalContext"]
    assert store.pending() == []          # queue consumed by the hook


def test_hook_silent_when_empty(store):
    r = _run_hook(store.path())
    assert r.returncode == 0
    assert r.stdout.strip() == ""         # no output → no injection


# ── runner integration ───────────────────────────────────────────────────────

def _runner(monkeypatch, store):
    import operator_agent as OA
    monkeypatch.setattr(OA, "operator_steer", store, raising=False)
    r = OA.AgentRunner()
    # steer() calls _save_state — keep it off the real operator-state.json
    r._state_path = os.path.join(os.path.dirname(store.path()), "state.json")
    r._transcript = []
    return r


def _fake_running(r):
    r.state = "running"
    r.bot = "claude-a"
    r._runtime = "claude"
    r._proc = types.SimpleNamespace(poll=lambda: None, pid=0)


def test_steer_requires_running(store, monkeypatch):
    r = _runner(monkeypatch, store)
    out = r.steer("hello?")
    assert not out["ok"]


def test_steer_queues_and_surfaces_in_trace(store, monkeypatch):
    r = _runner(monkeypatch, store)
    _fake_running(r)
    out = r.steer("use the mobile site")
    assert out["ok"] and out["queued"] == 1
    assert [s["text"] for s in store.pending()] == ["use the mobile site"]
    # visible in the run trace (history) and the shared transcript (continuity)
    assert any(m["role"] == "user" and m["text"] == "use the mobile site"
               for m in r.messages)
    assert r._transcript[-1] == {"role": "user", "text": "use the mobile site"}
    assert r.snapshot()["steer_pending"] == 1


def test_steer_rejects_garbage(store, monkeypatch):
    r = _runner(monkeypatch, store)
    _fake_running(r)
    assert not r.steer("")["ok"]
    assert not r.steer("x" * 10_000)["ok"]


def test_start_and_stop_clear_stale_queue(store, monkeypatch):
    import operator_agent as OA
    r = _runner(monkeypatch, store)
    store.push("stale steer from a dead run")
    # a fresh start must not leak old steers into the new run
    monkeypatch.setitem(OA.AGENT_BOTS, "claude-a",
                        dict(OA.AGENT_BOTS["claude-a"]))
    monkeypatch.setattr(r, "_start_locked",
                        lambda *a, **k: {"ok": True, "bot": "claude-a"})
    r.start("claude-a", "new task")
    assert store.pending() == []
    _fake_running(r)
    r.steer("mid-run steer")
    # the run's proc is gone by stop time (never killpg a fake pid — pid 0
    # would resolve to the TEST's own process group); stop() takes the
    # stale-running unwedge path, which must still abandon the queue
    r._proc = None
    r.stop()                              # stop abandons the queued steer too
    assert store.pending() == []


def test_exit_seam_returns_followup_prompt(store, monkeypatch):
    """A clean exit with steers still queued → one more resumed turn carrying
    them (the codex/agy path, and the claude race where the steer landed after
    the last tool call)."""
    r = _runner(monkeypatch, store)
    _fake_running(r)
    r.steer("also check shipping cost")
    r._proc = None
    p = r._steer_followup_check()
    assert "also check shipping cost" in p
    assert r._gate_pending is True        # proc-less gap reads as running
    assert store.pending() == []          # consumed into the follow-up prompt
    assert r._steer_followup_check() == ""   # queue drained → no more turns


def test_exit_seam_respects_stop_and_demo(store, monkeypatch):
    r = _runner(monkeypatch, store)
    _fake_running(r)
    r.steer("late steer")
    r._stopped = True
    assert r._steer_followup_check() == ""
    r._stopped = False
    r.demo = True
    assert r._steer_followup_check() == ""


def test_run_loop_chains_gate_then_steers(store, monkeypatch):
    """_run consumes follow-ups in a loop: a gate prompt, then steer prompts
    until the queue is dry — and a cancel breaks the chain as interrupted."""
    r = _runner(monkeypatch, store)
    calls = []

    def fake_inner(binpath, b, task):
        calls.append(task)
        if len(calls) == 1:
            return "GATE-PROMPT"          # §3.3 gate fires once
        if len(calls) == 2:
            store.push("steer landed during gate turn")
            r._proc = None
            return r._steer_followup_check()
        return None

    monkeypatch.setattr(r, "_run_inner", fake_inner)
    r.state = "running"
    r._run("bin", {}, "the task")
    assert calls[0] == "the task"
    assert calls[1] == "GATE-PROMPT"
    assert "steer landed during gate turn" in calls[2]
    assert len(calls) == 3                # queue dry → loop ends


def test_run_loop_cancel_breaks_chain(store, monkeypatch):
    r = _runner(monkeypatch, store)
    calls = []

    def fake_inner(binpath, b, task):
        calls.append(task)
        r._cancel_requested = True        # user hit Stop in the gap
        return "GATE-PROMPT"

    monkeypatch.setattr(r, "_run_inner", fake_inner)
    r.state = "running"
    r._run("bin", {}, "the task")
    assert calls == ["the task"]
    assert r.state == "interrupted"


# ── the hook settings writer ─────────────────────────────────────────────────

def test_settings_writer_idempotent_and_merge_safe(store, tmp_path, monkeypatch):
    import operator_agent as OA
    cwd = tmp_path / "botcwd"
    cwd.mkdir()
    sp = cwd / ".claude" / "settings.json"
    OA._ensure_steer_hook_settings(str(cwd))
    cfg = json.loads(sp.read_text())
    hooks = cfg["hooks"]["PostToolUse"]
    assert any("steer_hook.py" in h["command"]
               for grp in hooks for h in grp["hooks"])
    before = sp.read_text()
    OA._ensure_steer_hook_settings(str(cwd))     # second call: no churn
    assert sp.read_text() == before
    # merge-safe: user keys survive
    cfg["model"] = "claude-sonnet-5"
    sp.write_text(json.dumps(cfg))
    OA._ensure_steer_hook_settings(str(cwd))
    assert json.loads(sp.read_text())["model"] == "claude-sonnet-5"


# ── the /operator/agent/say route ────────────────────────────────────────────

def _app(monkeypatch, demo=False):
    from flask import Flask
    from jinja2 import ChoiceLoader, DictLoader
    if demo:
        os.environ["OPERATOR_DEMO"] = "1"
    else:
        os.environ.pop("OPERATOR_DEMO", None)
    import operator_view as OV
    mod = importlib.reload(OV)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mod.bp)
    app.jinja_loader = ChoiceLoader([
        app.jinja_loader,
        DictLoader({"_base.html": "<!doctype html>{% block title %}"
                                  "{% endblock %}{% block content %}{% endblock %}"})])
    return app, mod


def test_say_route_validation_and_states(store, monkeypatch):
    app, mod = _app(monkeypatch)
    c = app.test_client()
    assert c.post("/operator/agent/say", json={}).status_code == 400
    assert c.post("/operator/agent/say",
                  json={"text": "x" * 5000}).status_code == 413
    # idle runner → 409 (client falls back to a normal dispatch)
    assert c.post("/operator/agent/say",
                  json={"text": "hello"}).status_code == 409
    # running → 200 ok
    import operator_agent as OA
    monkeypatch.setattr(OA.runner, "steer",
                        lambda t: {"ok": True, "queued": 1, "live": True})
    body = c.post("/operator/agent/say", json={"text": "hello"})
    assert body.status_code == 200 and body.get_json()["ok"]


def test_snapshot_exposes_live_token_burn(store, monkeypatch):
    """1.0.15: the ledger's token numbers are visible WHILE the run burns."""
    r = _runner(monkeypatch, store)
    r._cum_in_tokens = 2_400_000
    r._peak_in_tokens = 900_000
    snap = r.snapshot()
    assert snap["cum_in_tokens"] == 2_400_000
    assert snap["peak_in_tokens"] == 900_000


def test_demo_default_paths_are_scoped(monkeypatch):
    """Review 2026-07-11 HIGH: the demo server runs as the same user — with no
    explicit path env, its steer/state/history/session defaults must NOT be
    the production files (a shared steer queue let a demo visitor inject into
    a live production run)."""
    import importlib
    monkeypatch.delenv("OPERATOR_STEER_PATH", raising=False)
    monkeypatch.setenv("OPERATOR_DEMO", "1")
    assert importlib.reload(OSTEER).path().endswith(".demo")
    monkeypatch.delenv("OPERATOR_DEMO", raising=False)
    assert not importlib.reload(OSTEER).path().endswith(".demo")
