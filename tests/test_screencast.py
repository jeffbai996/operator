"""Pushed frames (Page.startScreencast) replace polled screenshots.

Polled Page.captureScreenshot forced a raster and readback per frame: ~1.4 s
each under the stream's own load, and every tap waited behind one (measured
2026-10-19). A screencast costs nothing on a still page and arrives at the
compositor's rate on a moving one. Hermetic: a fake CDP session records sends
and delivers frame events; no browser.
"""
import asyncio
import base64
import time

import pytest

import operator_view as OV


class FakeSession:
    def __init__(self, fail_start=False):
        self.calls = []
        self.handlers = {}
        self.fail_start = fail_start

    def on(self, event, handler):
        self.handlers[event] = handler

    async def send(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "Page.startScreencast" and self.fail_start:
            raise RuntimeError("no screencast here")
        return {}

    def push(self, data=b"\xff\xd8pushed", sid=7, width=1280, height=964):
        self.handlers["Page.screencastFrame"]({
            "data": base64.b64encode(data).decode(), "sessionId": sid,
            "metadata": {"deviceWidth": width, "deviceHeight": height}})


class FakePage:
    url = "https://example.test/"


@pytest.fixture
def st(monkeypatch):
    monkeypatch.setattr(OV, "SCREENCAST", True)
    s = OV._Streamer()
    s._page = FakePage()
    s._target_ids[s._page] = "T1"
    return s


def test_a_start_asks_for_the_tier_and_a_frame_is_published(st):
    sess = FakeSession()
    assert asyncio.run(st._screencast_ensure(st._page, sess)) is True
    method, params = sess.calls[-1]
    assert method == "Page.startScreencast"
    assert params["format"] == "jpeg" and params["quality"] == OV.JPEG_QUALITY
    assert "maxWidth" not in params            # hi tier: full device resolution
    before = st.frame_id

    async def deliver():
        sess.push()
        await asyncio.sleep(0)                 # let the ack task run
    asyncio.run(deliver())
    assert st.frame == b"\xff\xd8pushed"
    assert st.frame_id == before + 1
    assert (st._capture_width, st._capture_height) == (1280, 964)
    assert st._frame_snapshot.target_id == "T1"
    assert st.cur_url == "https://example.test/"
    # the frame was acknowledged so Chrome sends the next one
    assert ("Page.screencastFrameAck", {"sessionId": 7}) in sess.calls


def test_lower_tiers_cap_the_pushed_frame_width(st):
    st.tier = "lo"
    sess = FakeSession()
    asyncio.run(st._screencast_ensure(st._page, sess))
    params = sess.calls[-1][1]
    assert params["quality"] == OV.TIER_LO_QUALITY and params["maxWidth"] == OV.TIER_LO_MAX_W
    # a tier change restarts the screencast with the new parameters
    st.tier = "hi"
    asyncio.run(st._screencast_ensure(st._page, sess))
    methods = [m for m, _ in sess.calls]
    assert methods[-2:] == ["Page.stopScreencast", "Page.startScreencast"]
    assert "maxWidth" not in sess.calls[-1][1]


def test_a_frame_for_another_page_is_acked_but_not_shown(st):
    sess = FakeSession()
    asyncio.run(st._screencast_ensure(st._page, sess))
    st._page = FakePage()                      # the stream moved on
    async def deliver():
        sess.push(data=b"\xff\xd8stale", sid=9)
        await asyncio.sleep(0)
    asyncio.run(deliver())
    assert st.frame != b"\xff\xd8stale"
    assert ("Page.screencastFrameAck", {"sessionId": 9}) in sess.calls


def test_a_page_switch_stops_the_old_screencast(st):
    old, new = FakeSession(), FakeSession()
    asyncio.run(st._screencast_ensure(st._page, old))
    other = FakePage()
    assert asyncio.run(st._screencast_ensure(other, new)) is True
    assert ("Page.stopScreencast", {}) in old.calls
    assert new.calls[-1][0] == "Page.startScreencast"
    assert st._sc_page is other


def test_a_failed_start_falls_back_to_polling_for_a_while(st):
    sess = FakeSession(fail_start=True)
    assert asyncio.run(st._screencast_ensure(st._page, sess)) is False
    assert st._sc_failed_until > time.monotonic()
    assert asyncio.run(st._screencast_ensure(st._page, FakeSession())) is False  # still backing off


def test_a_silent_screencast_is_abandoned(st, monkeypatch):
    sess = FakeSession()
    asyncio.run(st._screencast_ensure(st._page, sess))
    st._sc_started_at -= OV.SCREENCAST_FIRST_FRAME_S + 1   # no frame ever came
    assert asyncio.run(st._screencast_ensure(st._page, sess)) is False
    assert ("Page.stopScreencast", {}) in sess.calls
    assert st._sc_failed_until > time.monotonic()


def test_the_flag_restores_the_polled_path(st, monkeypatch):
    monkeypatch.setattr(OV, "SCREENCAST", False)
    sess = FakeSession()
    assert asyncio.run(st._screencast_ensure(st._page, sess)) is False
    assert sess.calls == []


def test_stop_is_bounded_and_idempotent(st):
    class Hanging(FakeSession):
        async def send(self, method, params=None):
            await asyncio.sleep(5)
    sess = Hanging()
    st._sc_sess, st._sc_page = sess, st._page
    monkey_timeout = OV.asyncio.wait_for
    started = time.monotonic()
    asyncio.run(st._screencast_stop())
    assert time.monotonic() - started < 2.5
    assert st._sc_sess is None
    asyncio.run(st._screencast_stop())          # nothing left to stop
