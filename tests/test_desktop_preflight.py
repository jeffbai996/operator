"""desktop-real dispatch pre-flight — the locked-console guard.

Found 2026-07-11: with the Windows console locked, win_capture happily returns
a phantom BLANK 1024×768 screen ('1024 768 1024 768', ~3KB white PNG). A
desktop-real run dispatched in that state burns the whole run clicking into a
white void. The dispatch route now probes the capture geometry first and
refuses with a clear error instead.
"""
import importlib
import os
import types

import pytest
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader

import operator_view as OV
import operator_agent as OA

_STUB_BASE = ("<!doctype html><title>{% block title %}{% endblock %}</title>"
              "{% block content %}{% endblock %}")


def _app(monkeypatch):
    os.environ.pop("OPERATOR_DEMO", None)
    mod = importlib.reload(OV)
    # dispatch pokes the browser streamer before starting a run — keep it inert
    monkeypatch.setattr(mod._streamer, "_ensure_chrome_alive", lambda: None)
    monkeypatch.setattr(mod._streamer, "ensure_running", lambda: None)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mod.bp)
    app.jinja_loader = ChoiceLoader([app.jinja_loader,
                                     DictLoader({"_base.html": _STUB_BASE})])
    return app, mod


def _fake_wb(size):
    wb = types.SimpleNamespace()
    wb.calls = []

    def screen_size(_target=None):
        wb.calls.append("screen_size")
        return size
    def ensure_input():
        wb.calls.append("ensure_input")
    wb.screen_size = screen_size
    wb.ensure_input = ensure_input
    return wb


@pytest.fixture()
def started(monkeypatch):
    """Record runner.start calls; return the list."""
    calls = []
    monkeypatch.setattr(OA.runner, "start",
                        lambda *a, **k: (calls.append((a, k)) or
                                         {"ok": True, "bot": "stub"}))
    return calls


def test_desktop_real_refused_when_console_phantom(monkeypatch, started):
    app, mod = _app(monkeypatch)
    wb = _fake_wb((1024, 768))
    monkeypatch.setattr(mod, "_load_cu", lambda f: wb)
    mod._active_surface["name"] = "browser"
    r = app.test_client().post("/operator/dispatch",
                               json={"task": "click things",
                                     "surface": "desktop-real", "real_ok": True})
    assert r.status_code == 409
    assert "locked" in r.get_json()["error"].lower()
    assert started == [], "a refused dispatch must not start a run"
    assert mod._active_surface["name"] == "browser", \
        "a refused dispatch must not flip the live feed surface"


def test_desktop_real_passes_on_real_geometry(monkeypatch, started):
    app, mod = _app(monkeypatch)
    wb = _fake_wb((1280, 882))
    monkeypatch.setattr(mod, "_load_cu", lambda f: wb)
    r = app.test_client().post("/operator/dispatch",
                               json={"task": "click things",
                                     "surface": "desktop-real", "real_ok": True})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert len(started) == 1
    assert wb.calls == ["ensure_input", "screen_size"]


def test_browser_dispatch_skips_probe(monkeypatch, started):
    app, mod = _app(monkeypatch)
    wb = _fake_wb((1280, 882))
    monkeypatch.setattr(mod, "_load_cu", lambda f: wb)
    r = app.test_client().post("/operator/dispatch",
                               json={"task": "browse", "surface": "browser"})
    assert r.status_code == 200
    assert wb.calls == [], "browser runs must not pay the capture probe"


def test_probe_failure_is_a_clean_409(monkeypatch, started):
    app, mod = _app(monkeypatch)

    def _boom(f):
        raise RuntimeError("powershell exploded")
    monkeypatch.setattr(mod, "_load_cu", _boom)
    r = app.test_client().post("/operator/dispatch",
                               json={"task": "click things",
                                     "surface": "desktop-real", "real_ok": True})
    assert r.status_code == 409
    assert started == []
