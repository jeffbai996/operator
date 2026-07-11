"""Server-side single session — one shared cockpit session across devices.

The chat log used to live only in each browser's localStorage: open the
cockpit on the iPad and the phone and you get two unrelated histories. The
live cockpit now persists ONE session server-side; every boot adopts it.
The public demo keeps per-visitor localStorage — these routes are 403 there.
"""
import importlib
import json
import os

import pytest
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader

import operator_session as OS_MOD
import operator_view as OV

_STUB_BASE = ("<!doctype html><title>{% block title %}{% endblock %}</title>"
              "{% block content %}{% endblock %}")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_SESSION_PATH", str(tmp_path / "session.json"))
    return importlib.reload(OS_MOD)


def _app(demo: bool, tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_SESSION_PATH", str(tmp_path / "session.json"))
    importlib.reload(OS_MOD)
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
    return app


# ------------------------------------------------------------- store unit --

def test_store_round_trip(store):
    assert store.load() == {"rev": 0, "data": None}
    r1 = store.save({"log": "<div>hi</div>", "mode": "auto"})
    assert r1 == 1
    got = store.load()
    assert got["rev"] == 1 and got["data"]["mode"] == "auto"
    assert store.save({"log": "x"}) == 2


def test_store_survives_corrupt_file(store, tmp_path):
    (tmp_path / "session.json").write_text("{not json")
    assert store.load() == {"rev": 0, "data": None}
    assert store.save({"log": "fresh"}) == 1     # corrupt file is overwritten


def test_store_rejects_oversize(store):
    with pytest.raises(ValueError):
        store.save({"log": "x" * (store.MAX_BYTES + 1)})


# ----------------------------------------------------------------- routes --

def test_session_routes_round_trip(tmp_path, monkeypatch):
    app = _app(False, tmp_path, monkeypatch)
    c = app.test_client()
    r = c.get("/operator/session")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "rev": 0,
                                                     "data": None}
    r = c.post("/operator/session",
               json={"data": {"log": "<div>from ipad</div>", "mode": "man"}})
    assert r.status_code == 200 and r.get_json()["rev"] == 1
    r = c.get("/operator/session")
    body = r.get_json()
    assert body["rev"] == 1 and body["data"]["log"] == "<div>from ipad</div>"


def test_session_post_requires_data_object(tmp_path, monkeypatch):
    app = _app(False, tmp_path, monkeypatch)
    c = app.test_client()
    assert c.post("/operator/session", json={}).status_code == 400
    assert c.post("/operator/session", json={"data": "not a dict"}).status_code == 400


def test_session_post_oversize_is_413(tmp_path, monkeypatch):
    app = _app(False, tmp_path, monkeypatch)
    c = app.test_client()
    r = c.post("/operator/session",
               json={"data": {"log": "x" * (OS_MOD.MAX_BYTES + 10)}})
    assert r.status_code == 413
    assert c.get("/operator/session").get_json()["rev"] == 0   # nothing saved


def test_session_routes_demo_gated(tmp_path, monkeypatch):
    app = _app(True, tmp_path, monkeypatch)
    c = app.test_client()
    assert c.get("/operator/session").status_code == 403
    assert c.post("/operator/session",
                  json={"data": {"log": "x"}}).status_code == 403
