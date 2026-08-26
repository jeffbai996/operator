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
        monkeypatch.setenv("OPERATOR_DEMO", "1")
    else:
        monkeypatch.delenv("OPERATOR_DEMO", raising=False)
    mod = importlib.reload(OV)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mod.bp)
    app.jinja_loader = ChoiceLoader([app.jinja_loader,
                                     DictLoader({"_base.html": _STUB_BASE})])
    return app


# ------------------------------------------------------------- store unit --

def test_store_round_trip(store):
    assert store.load() == {"rev": 0, "conversation_rev": 0, "data": None}
    r1 = store.save({"log": "<div>hi</div>", "mode": "auto"})
    assert r1 == 1
    got = store.load()
    assert got["rev"] == 1 and got["data"]["mode"] == "auto"
    assert store.save({"log": "x"}) == 2


def test_first_explicit_legacy_save_seeds_an_empty_store(store):
    assert store.save({"log": "first"}, conversation_id="legacy") == 1
    assert store.listing()["active"] == "legacy"
    assert store.load(conversation_id="legacy")["data"]["log"] == "first"


def test_store_survives_corrupt_file(store, tmp_path):
    (tmp_path / "session.json").write_text("{not json")
    assert store.load() == {"rev": 0, "conversation_rev": 0, "data": None}
    assert store.save({"log": "fresh"}) == 1     # corrupt file is overwritten


def test_store_rejects_oversize(store):
    with pytest.raises(ValueError):
        store.save({"log": "x" * (store.MAX_BYTES + 1)})


# ----------------------------------------------------------------- routes --

def test_session_routes_round_trip(tmp_path, monkeypatch):
    app = _app(False, tmp_path, monkeypatch)
    c = app.test_client()
    r = c.get("/operator/session")
    assert r.status_code == 200 and r.get_json() == {
        "ok": True, "rev": 0, "conversation_rev": 0,
        "data": None, "conversation_id": "legacy"}
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


# ------------------------------------------------------- conversations --
# One session was not enough (the owner 2026-08-06): every task landed in the same
# transcript and the only clean start was the trash can. The store now holds a
# MAP of conversations — without changing what load()/save() mean, so a client
# that never learns about them keeps working.

def test_saving_creates_the_first_conversation(store):
    assert store.listing()["sessions"] == []
    store.save({"log": "<div>hello</div>"})
    rows = store.listing()
    assert len(rows["sessions"]) == 1
    assert rows["active"] == rows["sessions"][0]["id"]
    assert rows["sessions"][0]["empty"] is False


def test_a_new_conversation_is_empty_and_active(store):
    store.save({"log": "<div>first errand</div>"})
    made = store.create()
    assert store.listing()["active"] == made["id"]
    assert store.load()["data"] is None          # the new one starts blank


def test_switching_back_returns_the_earlier_chat(store):
    store.save({"log": "<div>first errand</div>"})
    first = store.listing()["active"]
    store.create()
    store.save({"log": "<div>second errand</div>"})
    got = store.activate(first)
    assert got["data"]["log"] == "<div>first errand</div>"
    assert store.load()["data"]["log"] == "<div>first errand</div>"


def test_each_conversation_keeps_its_own_log(store):
    store.save({"log": "<div>A</div>"})
    a = store.listing()["active"]
    store.create()
    store.save({"log": "<div>B</div>"})
    b = store.listing()["active"]
    assert store.activate(a)["data"]["log"] == "<div>A</div>"
    assert store.activate(b)["data"]["log"] == "<div>B</div>"


def test_explicit_conversation_save_does_not_follow_global_active(store):
    store.save({"log": "A"})
    a = store.listing()["active"]
    b = store.create()["id"]
    store.save({"log": "B"}, conversation_id=b)
    store.activate(a)
    store.save({"log": "B updated from another browser"}, conversation_id=b)
    assert store.load(conversation_id=a)["data"]["log"] == "A"
    assert store.load(conversation_id=b)["data"]["log"] == "B updated from another browser"


def test_stale_device_cannot_overwrite_a_newer_conversation_revision(store):
    """Two devices can read the same thread, but only a save based on the
    current per-thread revision may replace it."""
    store.save({"log": "A"}, conversation_id="legacy")
    first = store.load(conversation_id="legacy")
    assert first["conversation_rev"] == 1

    store.save({"log": "desktop wins"}, conversation_id="legacy",
               expected_rev=first["conversation_rev"])
    with pytest.raises(store.SessionConflict) as caught:
        store.save({"log": "stale phone"}, conversation_id="legacy",
                   expected_rev=first["conversation_rev"])

    assert caught.value.current["conversation_rev"] == 2
    assert caught.value.current["data"]["log"] == "desktop wins"
    assert store.load(conversation_id="legacy")["data"]["log"] == "desktop wins"


def test_device_control_is_observer_first_and_takeover_is_explicit(store):
    store.save({"log": "thread"}, conversation_id="legacy")

    desktop = store.touch_presence("legacy", "desktop-a", "Windows")
    phone = store.touch_presence("legacy", "phone-b", "iPhone")
    assert desktop["can_control"] is True
    assert phone["can_control"] is False
    assert phone["controller_label"] == "Windows"

    taken = store.touch_presence(
        "legacy", "phone-b", "iPhone", take_over=True)
    old = store.touch_presence("legacy", "desktop-a", "Windows")
    assert taken["can_control"] is True
    assert taken["controller_label"] == "iPhone"
    assert old["can_control"] is False
    assert old["controller_label"] == "iPhone"


def test_abandoned_device_control_expires(store, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(store, "_clock", lambda: now[0])
    store.save({"log": "thread"}, conversation_id="legacy")
    assert store.touch_presence("legacy", "phone", "iPhone")["can_control"]

    now[0] += store.PRESENCE_TTL + 0.1
    claimed = store.touch_presence("legacy", "desktop", "Windows")
    assert claimed["can_control"] is True
    assert claimed["controller_label"] == "Windows"


def test_rename_sticks_and_is_clipped(store):
    store.save({"log": "x"})
    sid = store.listing()["active"]
    store.rename(sid, "  book   a flight  ")
    assert store.listing()["sessions"][0]["title"] == "book a flight"
    store.rename(sid, "y" * (store.TITLE_LIMIT + 50))
    assert len(store.listing()["sessions"][0]["title"]) == store.TITLE_LIMIT


def test_deleting_the_active_one_falls_through_to_a_survivor(store):
    store.save({"log": "A"})
    a = store.listing()["active"]
    store.create()
    store.save({"log": "B"})
    b = store.listing()["active"]
    out = store.delete(b)
    assert out["active"] == a
    assert store.load()["data"]["log"] == "A"


def test_deleting_the_last_one_leaves_somewhere_to_write(store):
    """The cockpit must always have an active conversation — a client that
    saves into nothing would lose the chat it just painted."""
    store.save({"log": "only"})
    only = store.listing()["active"]
    out = store.delete(only)
    assert out["active"] and out["active"] != only
    assert store.load()["data"] is None
    assert len(store.listing()["sessions"]) == 1


def test_deleting_an_unknown_conversation_raises(store):
    with pytest.raises(KeyError):
        store.delete("nope")


def test_a_v1_session_file_migrates_into_one_conversation(store, tmp_path):
    """The file on disk before this change is {rev, data} — it must come back
    as a real conversation, not as an empty cockpit."""
    (tmp_path / "session.json").write_text(
        json.dumps({"rev": 7, "data": {"log": "<div>from before</div>"}}))
    got = store.load()
    assert got["rev"] == 7 and got["data"]["log"] == "<div>from before</div>"
    rows = store.listing()
    assert len(rows["sessions"]) == 1 and rows["active"]


def test_a_legacy_file_keeps_a_stable_id_until_something_writes(store, tmp_path):
    """Migration runs on EVERY read until a write persists it. A fresh random
    id per read let a client list a conversation and then 404 activating it a
    second later — caught live on 2026-08-06, not by this suite."""
    (tmp_path / "session.json").write_text(
        json.dumps({"rev": 3, "data": {"log": "<div>before</div>"}}))
    first = store.listing()["sessions"][0]["id"]
    assert store.listing()["sessions"][0]["id"] == first
    assert store.activate(first)["data"]["log"] == "<div>before</div>"


def test_the_first_task_titles_the_conversation(store):
    store.save({"log": "x"})
    store.title_if_unset("book AC 8807 SEA to YVR")
    assert store.listing()["sessions"][0]["title"] == "book AC 8807 SEA to YVR"
    store.title_if_unset("something else entirely")
    assert store.listing()["sessions"][0]["title"] == "book AC 8807 SEA to YVR"


def test_auto_title_never_raises_without_a_conversation(store):
    store.title_if_unset("no active conversation yet")   # must not raise


# --------------------------------------------------- conversation routes --

def test_conversation_routes_round_trip(tmp_path, monkeypatch):
    app = _app(False, tmp_path, monkeypatch)
    c = app.test_client()
    c.post("/operator/session", json={"data": {"log": "<div>A</div>"}})
    first = c.get("/operator/sessions").get_json()["active"]

    made = c.post("/operator/sessions", json={}).get_json()
    assert made["ok"] and made["id"] != first
    c.post("/operator/session", json={"data": {"log": "<div>B</div>"}})

    r = c.post(f"/operator/sessions/{first}", json={"action": "activate"})
    assert r.get_json()["data"]["log"] == "<div>A</div>"

    assert c.post(f"/operator/sessions/{first}",
                  json={"action": "rename", "title": "errand one"}).status_code == 200
    rows = c.get("/operator/sessions").get_json()["sessions"]
    assert [s["title"] for s in rows if s["id"] == first] == ["errand one"]

    assert c.delete(f"/operator/sessions/{made['id']}").get_json()["active"] == first


def test_session_route_reads_and_writes_an_explicit_conversation(tmp_path, monkeypatch):
    app = _app(False, tmp_path, monkeypatch)
    c = app.test_client()
    c.post("/operator/session", json={"data": {"log": "A"}})
    a = c.get("/operator/sessions").get_json()["active"]
    b = c.post("/operator/sessions", json={}).get_json()["id"]
    c.post("/operator/session", json={"conversation_id": b,
                                      "data": {"log": "B"}})
    got_a = c.get(f"/operator/session?conversation_id={a}").get_json()
    got_b = c.get(f"/operator/session?conversation_id={b}").get_json()
    assert got_a["conversation_id"] == a and got_a["data"]["log"] == "A"
    assert got_b["conversation_id"] == b and got_b["data"]["log"] == "B"


def test_session_route_returns_conflict_payload_for_a_stale_device(
        tmp_path, monkeypatch):
    app = _app(False, tmp_path, monkeypatch)
    c = app.test_client()
    c.post("/operator/session", json={"conversation_id": "legacy",
                                      "data": {"log": "first"}})
    first = c.get("/operator/session?conversation_id=legacy").get_json()
    assert first["conversation_rev"] == 1

    won = c.post("/operator/session", json={
        "conversation_id": "legacy", "expected_rev": 1,
        "data": {"log": "desktop"}})
    assert won.status_code == 200
    stale = c.post("/operator/session", json={
        "conversation_id": "legacy", "expected_rev": 1,
        "data": {"log": "phone"}})
    body = stale.get_json()
    assert stale.status_code == 409
    assert body["conversation_rev"] == 2
    assert body["data"]["log"] == "desktop"


def test_presence_route_gates_writes_until_takeover(tmp_path, monkeypatch):
    app = _app(False, tmp_path, monkeypatch)
    c = app.test_client()
    c.post("/operator/session", json={"conversation_id": "legacy",
                                      "data": {"log": "first"}})
    url = "/operator/sessions/legacy/presence"
    assert c.post(url, json={"client_id": "desktop", "label": "Windows"}).get_json()[
        "can_control"] is True
    observing = c.post(url, json={"client_id": "phone", "label": "iPhone"})
    assert observing.get_json()["can_control"] is False

    refused = c.post("/operator/session", json={
        "conversation_id": "legacy", "client_id": "phone",
        "expected_rev": 1, "data": {"log": "stale phone"}})
    assert refused.status_code == 409
    assert "Windows" in refused.get_json()["error"]

    taken = c.post(url, json={"client_id": "phone", "label": "iPhone",
                              "take_over": True})
    assert taken.get_json()["can_control"] is True
    saved = c.post("/operator/session", json={
        "conversation_id": "legacy", "client_id": "phone",
        "expected_rev": 1, "data": {"log": "phone took over"}})
    assert saved.status_code == 200


def test_conversation_routes_reject_a_bad_id_and_action(tmp_path, monkeypatch):
    app = _app(False, tmp_path, monkeypatch)
    c = app.test_client()
    assert c.post("/operator/sessions/nope", json={}).status_code == 404
    assert c.delete("/operator/sessions/nope").status_code == 404
    c.post("/operator/session", json={"data": {"log": "x"}})
    sid = c.get("/operator/sessions").get_json()["active"]
    assert c.post(f"/operator/sessions/{sid}",
                  json={"action": "explode"}).status_code == 400
    assert c.post(f"/operator/sessions/{sid}",
                  json={"action": "rename"}).status_code == 400


def test_conversation_routes_demo_gated(tmp_path, monkeypatch):
    app = _app(True, tmp_path, monkeypatch)
    c = app.test_client()
    assert c.get("/operator/sessions").status_code == 403
    assert c.post("/operator/sessions", json={}).status_code == 403
    assert c.post("/operator/sessions/x", json={}).status_code == 403
    assert c.delete("/operator/sessions/x").status_code == 403
