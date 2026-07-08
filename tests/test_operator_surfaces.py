"""Track C tests — the surface axis in the Flask view: /operator/surfaces,
/operator/surface, dispatch pass-through, status surface field, demo lockout.

Same harness shape as test_operator_view.py: blueprint on a throwaway app,
runner mocked, module reloaded per DEMO flavor.

Run from the repo root:  PYTHONPATH=. pytest tests/test_operator_surfaces.py -q
"""
import importlib
import os

import pytest
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader

import operator_view as OV
import operator_agent as OA

_STUB_BASE = ("<!doctype html><title>{% block title %}{% endblock %}</title>"
              "{% block content %}{% endblock %}")


def _build_app(demo: bool):
    if demo:
        os.environ["OPERATOR_DEMO"] = "1"
    else:
        os.environ.pop("OPERATOR_DEMO", None)
    mod = importlib.reload(OV)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mod.bp)
    app.jinja_loader = ChoiceLoader([app.jinja_loader,
                                     DictLoader({"_base.html": _STUB_BASE})])
    return app, mod


class StartRecorder:
    """Stands in for operator_agent.runner — records start() kwargs."""

    def __init__(self):
        self.calls = []
        self.state = "idle"

    def start(self, bot, task, **kw):
        self.calls.append({"bot": bot, "task": task, **kw})
        return {"ok": True, "bot": bot}

    def is_running(self):
        return False

    def snapshot(self, since_ts=0.0):
        return {}

    def stop(self):
        return {"ok": True}


@pytest.fixture
def live():
    app, mod = _build_app(demo=False)
    rec = StartRecorder()
    orig = OA.runner
    OA.runner = rec
    # neutralize feed/streamer side effects
    mod._streamer.ensure_running = lambda: None
    mod._streamer._ensure_chrome_alive = lambda: None
    yield app.test_client(), mod, rec
    OA.runner = orig


@pytest.fixture
def demo():
    app, mod = _build_app(demo=True)
    rec = StartRecorder()
    orig = OA.runner
    OA.runner = rec
    mod._streamer.ensure_running = lambda: None
    mod._streamer._ensure_chrome_alive = lambda: None
    yield app.test_client(), mod, rec
    OA.runner = orig
    os.environ.pop("OPERATOR_DEMO", None)
    importlib.reload(OV)


# ── /operator/surfaces ───────────────────────────────────────────────────────
def test_surfaces_lists_all_three_live(live):
    c, mod, _ = live
    d = c.get("/operator/surfaces").get_json()
    keys = [s["key"] for s in d["surfaces"]]
    assert keys == ["browser", "desktop-sandbox", "desktop-real"]
    assert d["active"] == "browser"
    assert all("available" in s for s in d["surfaces"])


def test_surfaces_demo_is_browser_only(demo):
    c, mod, _ = demo
    d = c.get("/operator/surfaces").get_json()
    assert [s["key"] for s in d["surfaces"]] == ["browser"]


# ── /operator/surface (switch) ───────────────────────────────────────────────
def test_switch_unknown_surface_400(live):
    c, mod, _ = live
    r = c.post("/operator/surface", json={"surface": "hologram"})
    assert r.status_code == 400


def test_switch_desktop_real_requires_confirm(live):
    c, mod, _ = live
    mod._surface_available = lambda k: True
    r = c.post("/operator/surface", json={"surface": "desktop-real"})
    assert r.status_code == 403
    r2 = c.post("/operator/surface", json={"surface": "desktop-real",
                                           "confirm": True})
    assert r2.status_code == 200 and r2.get_json()["active"] == "desktop-real"
    mod._active_surface["name"] = "browser"          # reset module state


def test_switch_sandbox_updates_active_and_feed(live):
    c, mod, _ = live
    mod._surface_available = lambda k: True
    started = {}
    mod._desktop_feed.ensure_running = lambda s: started.setdefault("s", s)
    r = c.post("/operator/surface", json={"surface": "desktop-sandbox"})
    assert r.get_json()["ok"] and started["s"] == "desktop-sandbox"
    assert c.get("/operator/status").get_json()["surface"] == "desktop-sandbox"
    mod._active_surface["name"] = "browser"


def test_switch_unavailable_surface_409(live):
    c, mod, _ = live
    mod._surface_available = lambda k: k == "browser"
    r = c.post("/operator/surface", json={"surface": "desktop-sandbox"})
    assert r.status_code == 409


def test_switch_blocked_in_demo(demo):
    c, mod, _ = demo
    r = c.post("/operator/surface", json={"surface": "desktop-sandbox"})
    assert r.status_code == 403


# ── dispatch pass-through ────────────────────────────────────────────────────
def test_dispatch_passes_surface_and_real_ok(live):
    c, mod, rec = live
    mod._surface_available = lambda k: True
    r = c.post("/operator/dispatch", json={
        "bot": "claude-a", "task": "open the calculator",
        "surface": "desktop-sandbox"})
    assert r.status_code == 200
    assert rec.calls[-1]["surface"] == "desktop-sandbox"
    assert rec.calls[-1]["real_ok"] is False
    mod._active_surface["name"] = "browser"


def test_dispatch_demo_never_reaches_desktop(demo):
    c, mod, rec = demo
    r = c.post("/operator/dispatch", json={
        "bot": "claude-a", "task": "x", "surface": "desktop-real",
        "real_ok": True})
    assert r.status_code == 200
    # demo path calls start(demo=True) with NO surface kwarg — the runner
    # forces browser internally; the view must not forward the ask.
    assert rec.calls[-1].get("demo") is True
    assert "surface" not in rec.calls[-1]


# ── status ───────────────────────────────────────────────────────────────────
def test_status_reports_surface(live):
    c, mod, _ = live
    d = c.get("/operator/status").get_json()
    assert d["surface"] == "browser"
