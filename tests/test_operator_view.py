"""Characterization tests for operator_view.py — the Flask blueprint (30+ routes).

Purely additive: pins down CURRENT correct behavior of the routes via Flask's
test client. NO real Chrome, NO real agent runs, NO network — every seam that
would launch something (operator_agent.runner.start, the _Streamer's
run_action / list_tabs / ensure_running, filesystem event/transcript reads) is
mocked or stubbed. Cannot break the running cockpit.

Two app flavors are needed because DEMO gating is a module-level constant read
at import time (`DEMO = os.environ.get("OPERATOR_DEMO") == "1"`). We
importlib.reload the module under each env value to exercise the public-demo /
live-cockpit security boundary — the highest-value assertions here (demo forces
the locked bot/model, and saved tasks only serve a demo-scoped store).

Run (same shape as the sibling operator tests) from modules/operator:
  PYTHONPATH=. pytest tests/test_operator_view.py -q
"""
import importlib
import os

import pytest
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader

import operator_view as OV
import operator_agent as OA


# operator.html / operator_demo.html both `{% extends "_base.html" %}` — that base
# is provided by the parent host-app app in production, not by the blueprint's
# own template folder. Mounted standalone here it's missing, so supply a minimal
# stand-in so the page routes render (we assert status/headers, not markup).
_STUB_BASE = ("<!doctype html><title>{% block title %}{% endblock %}</title>"
              "{% block content %}{% endblock %}")


def _build_app(demo: bool):
    """Reload operator_view under the requested DEMO env, mount its blueprint on
    a throwaway Flask app, return (app, module). Reloading rebinds the module's
    DEMO constant + re-decorates the routes; it does NOT re-import operator_agent
    (operator_view only does `import operator_agent`), so a runner patched on
    OA.runner stays patched across the reload.

    NOTE: DEMO is a module-level global the routes read at request time, so only
    ONE flavor can be live at a time — a test must not hold a demo AND a live app
    simultaneously (the later reload wins for BOTH clients). Hence separate
    single-mode tests rather than dual-fixture ones."""
    if demo:
        os.environ["OPERATOR_DEMO"] = "1"
    else:
        os.environ.pop("OPERATOR_DEMO", None)
    mod = importlib.reload(OV)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mod.bp)
    # inject the stub _base.html alongside the blueprint's real templates
    app.jinja_loader = ChoiceLoader([app.jinja_loader,
                                     DictLoader({"_base.html": _STUB_BASE})])
    return app, mod


@pytest.fixture
def live():
    """Live-cockpit app (OPERATOR_DEMO unset). Restores module to live after."""
    app, mod = _build_app(demo=False)
    yield app.test_client(), mod
    _build_app(demo=False)   # leave the shared module in live mode for the next test


@pytest.fixture
def demo():
    """Public-demo app (OPERATOR_DEMO=1)."""
    app, mod = _build_app(demo=True)
    yield app.test_client(), mod
    _build_app(demo=False)   # always restore live so we don't leak DEMO into siblings


# ── runner / streamer fakes ──────────────────────────────────────────────────

class FakeRunner:
    """Stand-in for operator_agent.runner. Records start() calls so dispatch
    tests can assert the exact (bot, task, model, effort, demo) it was handed,
    and never launches a real agent."""
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []          # list of (args, kwargs) for start()
        self.stopped = False
        self.reset_bot = None

    def start(self, bot, task, model="", effort="", demo=False,
              surface="browser", real_ok=False):
        self.calls.append({"bot": bot, "task": task, "model": model,
                           "effort": effort, "demo": demo,
                           "surface": surface, "real_ok": real_ok})
        if self.ok:
            return {"ok": True, "bot": bot, "pid": 4242}
        return {"ok": False, "error": "already running"}

    def is_running(self):
        return False

    def stop(self):
        self.stopped = True
        return {"ok": True, "stopped": True}

    def reset_session(self, bot=""):
        self.reset_bot = bot
        return {"ok": True, "bot": bot}

    def snapshot(self, since_ts=0.0):
        return {"state": "idle", "messages": [], "since": since_ts}


class FakeStreamer:
    """Stand-in for _Streamer. Records run_action() payloads (for the steer
    whitelist test) and never attaches to Chrome. Only the surface the routes
    touch is implemented."""
    def __init__(self):
        self.status = "idle"
        self.detail = ""
        self.frame = None
        self.frame_ts = 0.0
        self.cur_url = ""
        self.vw = 0
        self.vh = 0
        self.last_view = 0.0
        self.last_click = (0.0, 0.0, 0.0)
        self._user_closed = False
        self.actions = []           # every dict passed to run_action
        self.tabs = []

    # routes call these — all inert
    def ensure_running(self):
        pass

    def _ensure_chrome_alive(self, relaunch=False):
        pass

    def run_action(self, action):
        self.actions.append(action)
        return {"ok": True, "url": "https://example.test", "echo": action}

    def list_tabs(self):
        return self.tabs

    def switch_tab(self, idx):
        return {"ok": True, "idx": idx}

    def close_tab(self, idx):
        return {"ok": True, "idx": idx}

    def new_tab(self):
        return {"ok": True}


@pytest.fixture
def fake_runner(monkeypatch):
    fr = FakeRunner()
    monkeypatch.setattr(OA, "runner", fr)
    return fr


@pytest.fixture
def fake_streamer(monkeypatch):
    """Swap the module-level _streamer singleton the routes reference. Patched on
    the live-imported OV; _build_app reloads OV, so tests that need this must
    patch AFTER the app fixture has reloaded — see _patch_streamer()."""
    return FakeStreamer()


def _patch_streamer(monkeypatch, mod, fs):
    monkeypatch.setattr(mod, "_streamer", fs)


# ═══════════════════════════════════════════════════════════════════════════
# 1. DEMO-mode gating — the public-demo / live-cockpit security boundary
# ═══════════════════════════════════════════════════════════════════════════

# Saved-task routes are live in BOTH flavors  — the demo runs
# them against a demo-scoped store (OPERATOR_TASKS_PATH) and fails closed (404)
# if that env is missing, so a visitor can never reach the owner's store.
TASK_ROUTES = [
    ("GET", "/operator/tasks"),
    ("POST", "/operator/tasks"),
    ("POST", "/operator/tasks/somewhere/run"),
    ("DELETE", "/operator/tasks/somewhere"),
]


@pytest.mark.parametrize("method,path", TASK_ROUTES)
def test_saved_task_routes_reachable_in_demo(demo, fake_runner, monkeypatch, method, path):
    # the owner 2026-07-09: the demo gets saved tasks too — against a demo-scoped
    # store (OPERATOR_TASKS_PATH), never the owner's. Reachable = no flat gate.
    client, mod = demo
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    monkeypatch.setenv("OPERATOR_TASKS_PATH", "/tmp/op-demo-tasks-test.json")
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "load_tasks", lambda: {})
    monkeypatch.setattr(OT, "get_task", lambda slug: None)
    monkeypatch.setattr(OT, "delete_task", lambda slug: False)
    monkeypatch.setattr(OT, "save_task", lambda d: (None, "empty name"))
    resp = client.open(path, method=method, json={})
    assert resp.status_code != 404 or (resp.get_json() or {}).get("error") != "not available"


def test_demo_task_run_applies_dispatch_lock(demo, fake_runner, monkeypatch):
    # a stored bundle can't smuggle a privileged bot/model/effort past the demo
    client, mod = demo
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    monkeypatch.setenv("OPERATOR_TASKS_PATH", "/tmp/op-demo-tasks-test.json")
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "get_task", lambda slug: {
        "prompt": "do the thing", "bot": "claude-a", "model": "opus",
        "effort": "max", "sites": [], "start_url": ""})
    monkeypatch.setattr(OT, "sites_preamble", lambda sites: "")
    monkeypatch.setattr(OT, "mark_run", lambda slug: None)
    resp = client.post("/operator/tasks/sneaky/run", json={})
    assert resp.status_code == 200
    call = fake_runner.calls[0]
    assert call["bot"] == "gemma"
    assert call["model"] == "Gemini 3.5 Flash (Low)"   # off-list stored model → default
    assert call["effort"] == ""
    assert call["demo"] is True


def test_demo_task_save_strips_bot_and_schedule(demo, monkeypatch):
    client, mod = demo
    monkeypatch.setenv("OPERATOR_TASKS_PATH", "/tmp/op-demo-tasks-test.json")
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "load_tasks", lambda: {})
    seen = {}
    monkeypatch.setattr(OT, "save_task", lambda d: (seen.update(d), ("x", None))[1])
    resp = client.post("/operator/tasks", json={
        "name": "N", "task": "P", "bot": "claude-a", "schedule": "0 9 * * *"})
    assert resp.status_code == 200
    assert seen["bot"] == ""          # dead field in demo (forced at run)
    assert seen["schedule"] == ""     # scheduler never runs on a public instance


def test_demo_task_store_cap(demo, monkeypatch):
    client, mod = demo
    monkeypatch.setenv("OPERATOR_TASKS_PATH", "/tmp/op-demo-tasks-test.json")
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    full = {f"t{i}": {"name": f"t{i}", "prompt": "p"} for i in range(mod.DEMO_TASKS_MAX)}
    monkeypatch.setattr(OT, "load_tasks", lambda: full)
    monkeypatch.setattr(OT, "save_task", lambda d: ("t0", None))
    # NEW task at cap → refused
    resp = client.post("/operator/tasks", json={"name": "new", "task": "p"})
    assert resp.status_code == 400
    assert "limit" in resp.get_json()["error"]
    # update-in-place of an EXISTING slug at cap → still fine
    resp = client.post("/operator/tasks", json={"slug": "t0", "name": "t0", "task": "p2"})
    assert resp.status_code == 200


@pytest.mark.parametrize("method,path", TASK_ROUTES)
def test_saved_task_routes_reachable_in_live(live, fake_runner, monkeypatch, method, path):
    client, mod = live
    fs = FakeStreamer()
    _patch_streamer(monkeypatch, mod, fs)
    # stub the tasks store so /run + list + delete don't hit the real ~/.cache file
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "load_tasks", lambda: {})
    monkeypatch.setattr(OT, "get_task", lambda slug: None)
    monkeypatch.setattr(OT, "delete_task", lambda slug: False)
    monkeypatch.setattr(OT, "save_task", lambda d: (None, "empty name"))
    resp = client.open(path, method=method, json={})
    # reachable = NOT the demo 404-refusal. It may legitimately 400/404-on-missing,
    # but never the flat demo gate. Assert it did real work (hit the store branch).
    assert resp.status_code != 404 or (resp.get_json() or {}).get("error") != "not available"


def test_unseen_is_zero_in_demo(demo):
    client, _ = demo
    assert client.get("/operator/unseen").get_json() == {"count": 0}


def test_drivers_generic_in_demo(demo):
    client, _ = demo
    dj = client.get("/operator/drivers").get_json()
    assert dj == {"drivers": [{"key": "bot", "label": "bot"}]}   # no the app names leak


def test_drivers_named_in_live(live):
    client, _ = live
    keys = {d["key"] for d in client.get("/operator/drivers").get_json()["drivers"]}
    assert "claude-a" in keys                                       # real drivers exposed


def test_models_locked_to_two_model_choice_in_demo(demo):
    # the owner 2026-07-09: Flash 3.5 Low default (first = picker default) + Sonnet
    # 4.6 as the only alt; tier baked into the value, effort control hidden.
    client, _ = demo
    assert client.get("/operator/models").get_json()["models"] == [
        {"value": "Gemini 3.5 Flash (Low)", "label": "3.5 Flash"},
        {"value": "Claude Sonnet 4.6 (Thinking)", "label": "Sonnet 4.6"},
    ]


def test_models_multiple_in_live(live):
    client, _ = live
    assert len(client.get("/operator/models").get_json()["models"]) >= 2


# ═══════════════════════════════════════════════════════════════════════════
# 2. /operator/dispatch — the agent-launch route
# ═══════════════════════════════════════════════════════════════════════════

def test_dispatch_live_calls_runner_with_expected_args(live, fake_runner, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    resp = client.post("/operator/dispatch", json={
        "bot": "claude-a", "task": "check the news", "model": "opus", "effort": "high"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert len(fake_runner.calls) == 1
    call = fake_runner.calls[0]
    assert call["bot"] == "claude-a"
    assert call["task"] == "check the news"
    assert call["model"] == "opus"
    assert call["effort"] == "high"
    assert call["demo"] is False        # live path never passes demo=True
    assert call["surface"] == "browser"  # Track C: default surface rides along
    assert call["real_ok"] is False


def test_dispatch_demo_forces_gemma_and_default_model_and_demo_true(demo, fake_runner, monkeypatch):
    client, mod = demo
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    # client TRIES to inject a privileged bot/model/effort; demo must ignore it
    resp = client.post("/operator/dispatch", json={
        "bot": "claude-a", "task": "do it", "model": "opus", "effort": "high"})
    assert resp.status_code == 200
    call = fake_runner.calls[0]
    assert call["bot"] == "gemma"                          # forced, client bot ignored
    assert call["model"] == "Gemini 3.5 Flash (Low)"       # off-list model → default
    assert call["effort"] == ""                            # lock owns effort (tier in model string)
    assert call["demo"] is True                            # strips the app context


def test_dispatch_demo_honors_sonnet_alt(demo, fake_runner, monkeypatch):
    # the ONE allowed alternative model passes through; everything else stays locked
    client, mod = demo
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    resp = client.post("/operator/dispatch", json={
        "bot": "claude-a", "task": "do it",
        "model": "Claude Sonnet 4.6 (Thinking)", "effort": "max"})
    assert resp.status_code == 200
    call = fake_runner.calls[0]
    assert call["bot"] == "gemma"
    assert call["model"] == "Claude Sonnet 4.6 (Thinking)"  # allowlisted alt honored
    assert call["effort"] == ""                            # injected effort still ignored
    assert call["demo"] is True


def test_dispatch_demo_sandbox_surface_forces_sonnet(demo, fake_runner, monkeypatch):
    # Flash has no computer-use tools : a demo sandbox run
    # always gets Sonnet, even when the visitor picked (or defaulted to) Flash.
    client, mod = demo
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    resp = client.post("/operator/dispatch", json={
        "bot": "x", "task": "open the editor", "model": "Gemini 3.5 Flash (Low)",
        "surface": "desktop-sandbox"})
    assert resp.status_code == 200
    call = fake_runner.calls[0]
    assert call["model"] == "Claude Sonnet 4.6 (Thinking)"
    assert call["surface"] == "desktop-sandbox"
    assert call["demo"] is True


def test_dispatch_empty_task_rejected_cleanly(live, fake_runner, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    resp = client.post("/operator/dispatch", json={"bot": "claude-a", "task": "   "})
    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": "empty task"}
    assert fake_runner.calls == []          # runner never invoked on a bad body


def test_dispatch_malformed_body_is_400_not_500(live, fake_runner, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    # garbage body / wrong content-type → get_json(silent=True) falls back to form,
    # task ends up empty → clean 400, NOT a 500.
    resp = client.post("/operator/dispatch", data="not json at all",
                       content_type="text/plain")
    assert resp.status_code == 400
    assert fake_runner.calls == []


def test_dispatch_runner_conflict_returns_409(live, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    monkeypatch.setattr(OA, "runner", FakeRunner(ok=False))
    resp = client.post("/operator/dispatch", json={"bot": "claude-a", "task": "go"})
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 3. /operator/steer — the action whitelist (regression guard for the
#    silently-dropped-field bug class: dx/dy scroll deltas, x0..y1 drag coords)
# ═══════════════════════════════════════════════════════════════════════════

# For each action kind, the fields _do_action actually consumes must survive the
# route's manual whitelist into the dict handed to run_action. If any are dropped
# the action silently degrades (the historical scroll-up / drag-to-origin bugs).
STEER_CASES = [
    # (posted body, kind, {field: expected_value_in_action})
    ("scroll dy", {"kind": "scroll", "dy": -600, "dx": 0}, "scroll",
     {"dy": -600, "dx": 0}),
    ("scroll dx", {"kind": "scroll", "dx": 120}, "scroll", {"dx": 120}),
    ("drag coords", {"kind": "drag", "x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.9},
     "drag", {"x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.9}),
    ("click_at xy", {"kind": "click_at", "x": 0.5, "y": 0.6}, "click_at",
     {"x": 0.5, "y": 0.6}),
    ("click_at count", {"kind": "click_at", "x": 0.5, "y": 0.6, "count": 3},
     "click_at", {"count": 3}),
    ("rclick_at xy", {"kind": "rclick_at", "x": 0.3, "y": 0.4}, "rclick_at",
     {"x": 0.3, "y": 0.4}),
    ("mousedown_at xy", {"kind": "mousedown_at", "x": 0.1, "y": 0.1},
     "mousedown_at", {"x": 0.1, "y": 0.1}),
    ("goto value", {"kind": "goto", "value": "example.test"}, "goto",
     {"value": "example.test"}),
    ("type value", {"kind": "type", "value": "hello"}, "type", {"value": "hello"}),
    ("key value", {"kind": "key", "value": "Enter"}, "key", {"value": "Enter"}),
]


@pytest.mark.parametrize("label,body,kind,expected", STEER_CASES,
                         ids=[c[0] for c in STEER_CASES])
def test_steer_whitelist_preserves_fields(live, monkeypatch, label, body, kind, expected):
    client, mod = live
    fs = FakeStreamer()
    _patch_streamer(monkeypatch, mod, fs)
    resp = client.post("/operator/steer", json=body)
    assert resp.status_code == 200
    assert len(fs.actions) == 1
    action = fs.actions[0]
    assert action["kind"] == kind
    for field, val in expected.items():
        assert field in action, f"{kind}: field '{field}' was dropped by the whitelist"
        assert action[field] == val, f"{kind}: field '{field}' mangled ({action[field]!r} != {val!r})"


def test_steer_scroll_delta_defaults_to_none_not_zero(live, monkeypatch):
    """The load-bearing subtlety behind the wheel-up bug: when NO dx/dy is posted,
    they must arrive as None (so _do_action distinguishes 'no delta → keyword'
    from 'a real 0 delta'). A 0 default would silently break keyword scrolls."""
    client, mod = live
    fs = FakeStreamer()
    _patch_streamer(monkeypatch, mod, fs)
    client.post("/operator/steer", json={"kind": "scroll", "value": "up"})
    action = fs.actions[0]
    assert action["dx"] is None
    assert action["dy"] is None
    assert action["value"] == "up"


def test_steer_missing_kind_is_400(live, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    resp = client.post("/operator/steer", json={"value": "x"})
    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": "missing action kind"}


def test_steer_accepts_form_encoded_body(live, monkeypatch):
    """Route reads get_json(silent=True) OR request.form — a form POST must work."""
    client, mod = live
    fs = FakeStreamer()
    _patch_streamer(monkeypatch, mod, fs)
    resp = client.post("/operator/steer", data={"kind": "type", "value": "hi"})
    assert resp.status_code == 200
    assert fs.actions[0]["kind"] == "type"
    assert fs.actions[0]["value"] == "hi"


# ═══════════════════════════════════════════════════════════════════════════
# 4. /operator/status + /operator/tasks — happy path + missing-state resilience
# ═══════════════════════════════════════════════════════════════════════════

def test_status_happy_path_shape(live, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    fs.status = "live"
    fs.cur_url = "https://example.test"
    fs.vw, fs.vh = 1280, 800
    _patch_streamer(monkeypatch, mod, fs)
    # keep clear_unseen from touching the real schedule module/file
    import operator_schedule as OS
    monkeypatch.setattr(OS, "clear_unseen", lambda: None)
    resp = client.get("/operator/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"status", "detail", "has_frame", "vw", "vh", "url", "click", "surface"}
    assert body["status"] == "live"
    assert body["url"] == "https://example.test"
    assert body["vw"] == 1280 and body["vh"] == 800
    assert body["has_frame"] is False        # no frame set → not fresh


def test_status_survives_schedule_module_blowup(live, monkeypatch):
    """clear_unseen() is wrapped in try/except — a broken schedule import must not
    500 the status poll (the cockpit polls this every second)."""
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    import operator_schedule as OS
    def boom():
        raise RuntimeError("schedule store gone")
    monkeypatch.setattr(OS, "clear_unseen", boom)
    resp = client.get("/operator/status")
    assert resp.status_code == 200            # swallowed, not surfaced


def test_tasks_list_happy_path(live, monkeypatch):
    client, mod = live
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "load_tasks", lambda: {
        "morning": {"name": "Morning", "prompt": "do", "created": 1, "last_run": None},
    })
    resp = client.get("/operator/tasks")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert len(body["tasks"]) == 1
    t = body["tasks"][0]
    assert t["slug"] == "morning"
    assert t["name"] == "Morning"
    # _task_public projects a stable shape
    assert set(t) >= {"slug", "name", "prompt", "sites", "bot", "model",
                      "effort", "start_url", "schedule", "created", "last_run"}


def test_tasks_list_empty_when_store_missing(live, monkeypatch):
    client, mod = live
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "load_tasks", lambda: {})   # store.py already no-raises
    resp = client.get("/operator/tasks")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "tasks": []}


def test_tasks_post_rejects_empty_name(live, monkeypatch):
    client, mod = live
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "save_task", lambda d: (None, "empty name"))
    resp = client.post("/operator/tasks", json={"name": "", "task": "x"})
    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": "empty name"}


def test_task_run_missing_slug_is_404_not_500(live, fake_runner, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "get_task", lambda slug: None)      # no such task
    resp = client.post("/operator/tasks/ghost/run", json={})
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "no such task"}
    assert fake_runner.calls == []


def test_task_run_dispatches_and_marks_run(live, fake_runner, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    _patch_streamer(monkeypatch, mod, fs)
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "get_task", lambda slug: {
        "prompt": "read the filings", "bot": "claude-a", "model": "opus",
        "effort": "high", "sites": [], "start_url": ""})
    monkeypatch.setattr(OT, "sites_preamble", lambda sites: "")
    marked = []
    monkeypatch.setattr(OT, "mark_run", lambda slug: marked.append(slug))
    resp = client.post("/operator/tasks/deepdive/run", json={})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert fake_runner.calls[0]["task"] == "read the filings"
    assert fake_runner.calls[0]["bot"] == "claude-a"
    assert marked == ["deepdive"]           # last_run stamped only on ok dispatch


# ═══════════════════════════════════════════════════════════════════════════
# 5. tabs + agent passthrough routes (thin wrappers → streamer/runner)
# ═══════════════════════════════════════════════════════════════════════════

def test_tabs_route_returns_streamer_snapshot(live, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    fs.tabs = [{"i": 0, "title": "Google", "url": "https://g.test", "active": True}]
    _patch_streamer(monkeypatch, mod, fs)
    resp = client.get("/operator/tabs")
    assert resp.status_code == 200
    assert resp.get_json()["tabs"] == fs.tabs


def test_tab_switch_close_new_are_post_only(live, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    assert client.get("/operator/tab/0").status_code == 405          # GET not allowed
    assert client.post("/operator/tab/0").get_json() == {"ok": True, "idx": 0}
    assert client.post("/operator/tab/1/close").get_json() == {"ok": True, "idx": 1}
    assert client.post("/operator/tab/new").get_json() == {"ok": True}


def test_agent_stop_and_reset_delegate_to_runner(live, fake_runner):
    client, _ = live
    assert client.post("/operator/agent/stop").get_json()["stopped"] is True
    assert fake_runner.stopped is True
    client.post("/operator/agent/reset", json={"bot": "claude-a"})
    assert fake_runner.reset_bot == "claude-a"


def test_agent_snapshot_parses_since_and_tolerates_garbage(live, fake_runner):
    client, _ = live
    ok = client.get("/operator/agent?since=1234.5").get_json()
    assert ok["since"] == 1234.5
    # non-numeric `since` must not 500 — the route coerces to 0.0
    bad = client.get("/operator/agent?since=notanumber")
    assert bad.status_code == 200
    assert bad.get_json()["since"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 6. pure-ish helpers — event tail + driver detection (no Chrome, filesystem seam)
# ═══════════════════════════════════════════════════════════════════════════

def test_recent_events_reads_ndjson_tail(live, monkeypatch, tmp_path):
    _, mod = live
    log = tmp_path / "events.ndjson"
    log.write_text('{"bot":"claude-a","action":"click","ts":1}\n'
                   'bad line that is not json\n'
                   '{"bot":"gpt","action":"type","ts":2}\n', encoding="utf-8")
    monkeypatch.setattr(mod, "_EVENT_LOG", str(log))
    evs = mod._recent_events(40)
    assert len(evs) == 2                     # the malformed line is skipped
    assert evs[-1]["bot"] == "gpt"


def test_recent_events_missing_file_returns_empty(live, monkeypatch, tmp_path):
    _, mod = live
    monkeypatch.setattr(mod, "_EVENT_LOG", str(tmp_path / "nope.ndjson"))
    assert mod._recent_events() == []        # OSError swallowed → []


def test_current_driver_within_window(live, monkeypatch):
    import time
    _, mod = live
    now = time.time()
    monkeypatch.setattr(mod, "_recent_events",
                        lambda n=8: [{"bot": "claude-a", "action": "click",
                                      "detail": "the button", "ts": now}])
    drv = mod._current_driver(window_s=12.0)
    assert drv["bot"] == "claude-a"
    assert drv["action"] == "click"


def test_current_driver_stale_is_none(live, monkeypatch):
    _, mod = live
    monkeypatch.setattr(mod, "_recent_events",
                        lambda n=8: [{"bot": "claude-a", "action": "click", "ts": 0}])
    assert mod._current_driver(window_s=12.0) is None   # ancient ts → nobody driving


def test_current_driver_masks_bot_name_in_demo(demo, monkeypatch):
    import time
    _, mod = demo
    now = time.time()
    monkeypatch.setattr(mod, "_recent_events",
                        lambda n=8: [{"bot": "claude-a", "action": "click", "ts": now}])
    drv = mod._current_driver()
    assert drv["bot"] == "assistant"        # never leak the real the app bot name


def test_assistant_text_extracts_from_content_blocks(live):
    _, mod = live
    # string content
    assert mod._assistant_text({"message": {"content": "hi there"}}) == "hi there"
    # block-list content — only text blocks, joined
    msg = {"message": {"content": [
        {"type": "text", "text": "first"},
        {"type": "tool_use", "name": "x"},
        {"type": "text", "text": "second"}]}}
    assert mod._assistant_text(msg) == "first second"
    # no content → empty
    assert mod._assistant_text({"message": {}}) == ""


def test_iso_epoch_parses_and_defaults_zero(live):
    _, mod = live
    assert mod._iso_epoch("2026-07-02T06:15:00+00:00") > 0
    assert mod._iso_epoch("") == 0.0
    assert mod._iso_epoch("garbage") == 0.0
    assert mod._iso_epoch(None) == 0.0


def test_slug_matches_claude_project_dir_convention(live):
    _, mod = live
    # abspath with / . _ all collapsed to '-'
    s = mod._slug("/home/user/agents/claude-a")
    assert s == "-home-user-agents-claude-a"
    assert "_" not in mod._slug("/tmp/a_b/c.d")


def test_shot_route_rejects_traversal_and_bad_ext(live, monkeypatch):
    client, mod = live
    # path traversal / non-basename → 404 before touching the filesystem
    assert client.get("/operator/shot/..%2f..%2fetc%2fpasswd").status_code == 404
    assert client.get("/operator/shot/notanimage.txt").status_code == 404
    assert client.get("/operator/shot/.hidden.png").status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 7. simple page routes
# ═══════════════════════════════════════════════════════════════════════════

def test_cockpit_redirects_to_operator(live):
    client, _ = live
    resp = client.get("/cockpit")
    assert resp.status_code in (301, 302, 308)
    assert "/operator" in resp.headers["Location"]


def test_operator_page_renders_and_is_no_store(live):
    client, _ = live
    resp = client.get("/operator")
    assert resp.status_code == 200
    assert "no-store" in resp.headers.get("Cache-Control", "")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ── /operator/frame — the pull half of the feed (anti buffer-bloat) ─────────
def test_frame_serves_placeholder_before_first_capture(live, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    mod._active_surface["name"] = "browser"
    r = client.get("/operator/frame")
    assert r.status_code == 200
    assert r.mimetype == "image/jpeg"
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Operator-Frame"] == "placeholder"


def test_frame_serves_newest_live_frame(live, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    fs.frame = b"\xff\xd8fake-jpeg-bytes\xff\xd9"
    _patch_streamer(monkeypatch, mod, fs)
    mod._active_surface["name"] = "browser"
    r = client.get("/operator/frame")
    assert r.status_code == 200
    assert r.headers["X-Operator-Frame"] == "live"
    assert r.data == fs.frame
    assert fs.last_view > 0     # a pull counts as viewing (feeds the idle-stop)


# ── feed self-heal: cycle a decayed sandbox stream ───────────────────────────
def test_stream_decay_decision():
    F = OV._DesktopFeed
    # young stream: never judged, even at zero frames
    assert F._stream_decayed(0, 6.0, age_s=3.0) is False
    # short window: not enough evidence
    assert F._stream_decayed(0, 2.0, age_s=60.0) is False
    # aged + sagging (measured decay: ~0.7fps vs configured 10) → cycle
    assert F._stream_decayed(3, 6.0, age_s=60.0) is True
    # aged + healthy (10fps) → keep
    assert F._stream_decayed(50, 5.0, age_s=600.0) is False
    # boundary: exactly the floor is NOT decayed (strict less-than)
    assert F._stream_decayed(20, 5.0, age_s=60.0) is False
