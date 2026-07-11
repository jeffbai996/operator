"""Flight recorder — the run-history ledger.

Every run's terminal state used to evaporate (only the last transcript
survived, overwritten per run). operator_history persists one SQLite row per
run — task, who ran it, on what surface, how it ended, token spend, trace —
written best-effort from the runner's terminal transition (a history failure
must NEVER break a live run).
"""
import importlib
import json
import time
import types

import pytest

import operator_history as OH


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_HISTORY_PATH", str(tmp_path / "history.db"))
    return importlib.reload(OH)


def _fake_runner(**over):
    r = types.SimpleNamespace(
        bot="claude-a", task="price a grocery run", state="done",
        model="claude-sonnet-5", effort="medium", surface="browser",
        demo=False, started_ts=time.time() - 42.0, ended_ts=time.time(),
        _runtime="claude", _cum_in_tokens=123_456, _peak_in_tokens=45_000,
        messages=[{"ts": time.time(), "role": "assistant", "text": "done!"}],
    )
    for k, v in over.items():
        setattr(r, k, v)
    return r


def test_record_and_read_back(store):
    rid = store.record(_fake_runner(), reason="exit 0")
    assert isinstance(rid, int)
    rows = store.recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["task"] == "price a grocery run"
    assert row["state"] == "done" and row["reason"] == "exit 0"
    assert row["cum_in_tokens"] == 123_456
    assert row["duration_s"] == pytest.approx(42.0, abs=2.0)
    assert "trace" not in row, "recent() rows are lean — no trace payload"
    full = store.get(rid)
    assert full["trace"][0]["text"] == "done!"


def test_recent_newest_first_and_limited(store):
    for i in range(5):
        store.record(_fake_runner(task=f"task {i}"), reason="exit 0")
    rows = store.recent(limit=3)
    assert [r["task"] for r in rows] == ["task 4", "task 3", "task 2"]


def test_record_never_raises_on_garbage(store):
    r = _fake_runner(messages=[{"ts": 1, "obj": object()}],   # unserializable
                     started_ts=None, ended_ts=None,
                     _cum_in_tokens="not a number")
    rid = store.record(r, reason="exit 0")
    assert rid is not None                    # degraded, but recorded
    assert store.get(rid)["task"] == "price a grocery run"


def test_trace_is_capped(store):
    msgs = [{"ts": i, "role": "assistant", "text": "x" * 4000}
            for i in range(500)]
    rid = store.record(_fake_runner(messages=msgs), reason="exit 0")
    blob = json.dumps(store.get(rid)["trace"])
    assert len(blob.encode()) <= store.MAX_TRACE_BYTES + 4096
    # the cap keeps the TAIL (the terminal turns are the interesting part)
    assert store.get(rid)["trace"][-1]["ts"] == 499


def test_get_unknown_id_is_none(store):
    assert store.get(99999) is None


def _app(demo, monkeypatch):
    import os
    from flask import Flask
    from jinja2 import ChoiceLoader, DictLoader
    import operator_view as OV
    if demo:
        os.environ["OPERATOR_DEMO"] = "1"
    else:
        os.environ.pop("OPERATOR_DEMO", None)
    mod = importlib.reload(OV)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mod.bp)
    app.jinja_loader = ChoiceLoader([
        app.jinja_loader,
        DictLoader({"_base.html": "<!doctype html>{% block title %}"
                                  "{% endblock %}{% block content %}{% endblock %}"})])
    return app


def test_history_routes_list_and_get(store, monkeypatch):
    r1 = store.record(_fake_runner(task="first"), reason="exit 0")
    store.record(_fake_runner(task="second", state="interrupted"),
                 reason="user stop")
    app = _app(False, monkeypatch)
    c = app.test_client()
    body = c.get("/operator/history").get_json()
    assert body["ok"] and [r["task"] for r in body["runs"]] == ["second", "first"]
    assert "trace" not in body["runs"][0]
    full = c.get(f"/operator/history/{r1}").get_json()
    assert full["ok"] and full["run"]["trace"][0]["text"] == "done!"
    assert c.get("/operator/history/424242").status_code == 404


def test_history_routes_demo_gated(store, monkeypatch):
    app = _app(True, monkeypatch)
    c = app.test_client()
    assert c.get("/operator/history").status_code == 403
    assert c.get("/operator/history/1").status_code == 403


def test_runner_terminal_transition_records_once(store, monkeypatch):
    """running→done records exactly once; repeat transitions and non-terminal
    transitions don't. Wired via operator_agent._set_state."""
    import operator_agent as OA
    calls = []
    monkeypatch.setattr(OA, "operator_history", store, raising=False)
    monkeypatch.setattr(store, "record", lambda r, reason="": calls.append(reason))
    runner = OA.AgentRunner()
    runner.state = "running"
    runner._set_state("done", "exit 0")
    assert calls == ["exit 0"]
    runner._set_state("done", "exit 0")       # already terminal — no re-record
    runner._set_state("idle")
    runner.state = "running"
    runner._set_state("interrupted", "user stop")
    assert calls == ["exit 0", "user stop"]
