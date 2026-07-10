"""1.0.8 F1/F2 — adaptive frame tier + eager post-action frames.

F1: a ?tier=lo client (narrow viewport / Save-Data) gets downscaled,
harder-compressed browser frames and a lower-rate sandbox stream. The
byte-ratio test against a real headless Chromium is the load-bearing proof
of the bandwidth win; the fake-session tests pin the CDP capture params.

F2: an input action wakes the capture loop immediately so the result paints
within a frame, with idle cadence unchanged (no steady-state bandwidth).

Run from modules/operator:  PYTHONPATH=. pytest tests/test_feed_tiers.py -q
"""
import asyncio
import base64
import os
import threading
import time

import pytest

import operator_view as OV


# ── F1: _grab capture params (hermetic, fake CDP session) ────────────────────

class _FakeSess:
    """Records CDP sends; answers captureScreenshot with a tiny valid payload."""
    def __init__(self):
        self.calls = []

    async def send(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"\xff\xd8fakejpeg").decode()}
        return {"result": {"value": "null"}}


def _grab_with(tier, vw=1400, vh=900):
    st = OV._Streamer()
    st.tier = tier
    st.vw, st.vh = vw, vh
    st._cdp = _FakeSess()
    out = asyncio.run(st._grab(object()))
    shot = next(p for m, p in st._cdp.calls if m == "Page.captureScreenshot")
    return out, shot


def test_grab_hi_params_unchanged():
    out, shot = _grab_with("hi")
    assert out == b"\xff\xd8fakejpeg"
    assert shot["quality"] == OV.JPEG_QUALITY
    assert "clip" not in shot


def test_grab_lo_downscales_and_compresses_harder():
    _, shot = _grab_with("lo", vw=1400, vh=900)
    assert shot["quality"] == OV.TIER_LO_QUALITY < OV.JPEG_QUALITY
    clip = shot["clip"]
    assert clip["width"] == 1400.0 and clip["height"] == 900.0
    assert clip["scale"] == pytest.approx(OV.TIER_LO_MAX_W / 1400.0)


def test_grab_lo_skips_clip_when_viewport_unknown():
    _, shot = _grab_with("lo", vw=0, vh=0)
    assert shot["quality"] == OV.TIER_LO_QUALITY
    assert "clip" not in shot     # nothing sane to scale against — quality only


def test_grab_lo_skips_clip_on_already_narrow_viewport():
    _, shot = _grab_with("lo", vw=800, vh=600)
    assert "clip" not in shot     # never upscale a small viewport


# ── F1: the load-bearing byte-ratio proof (real headless Chromium) ───────────

_CHROME = os.path.expanduser(
    "~/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")

_BUSY_HTML = """<!doctype html><body style="margin:0">
<div style="width:1400px;height:900px;background:
 repeating-linear-gradient(45deg,#c33,#38c 40px,#3c8 80px,#fc0 120px)">
%s</div></body>""" % "".join(
    f'<p style="color:#{i % 10}{i % 7}f;font:16px serif">frame tier probe {i} '
    f'lorem ipsum dolor sit amet consectetur</p>' for i in range(40))


_HAS_PLAYWRIGHT = True
try:
    import playwright  # noqa: F401 — present in the host-app/server venv
except ImportError:
    _HAS_PLAYWRIGHT = False


@pytest.mark.skipif(not (_HAS_PLAYWRIGHT and os.path.exists(_CHROME)),
                    reason="needs the playwright package (host-app venv) + "
                           "chromium binary; run: ../host-app/venv/bin/python "
                           "-m pytest tests/test_feed_tiers.py -k materially")
def test_lo_tier_frame_is_materially_smaller_than_hi():
    """THE bandwidth proof: on a Retina-scale page a lo-tier frame must be a
    fraction of the hi frame's bytes (downscale + quality together)."""
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path=_CHROME,
                                               headless=True)
            page = await browser.new_page(
                viewport={"width": 1400, "height": 900}, device_scale_factor=2)
            await page.set_content(_BUSY_HTML)
            st = OV._Streamer()
            st.vw, st.vh = 1400, 900
            st.tier = "hi"
            hi = await st._grab(page)
            st.tier = "lo"
            lo = await st._grab(page)
            await browser.close()
            return hi, lo

    hi, lo = asyncio.run(run())
    assert hi and lo and hi[:2] == b"\xff\xd8" and lo[:2] == b"\xff\xd8"
    assert len(lo) < len(hi) * 0.5, \
        f"lo tier not materially smaller: hi={len(hi)}B lo={len(lo)}B"


# ── F1: routes propagate the tier to both feed sources ───────────────────────

@pytest.fixture
def app(monkeypatch):
    from flask import Flask
    monkeypatch.setattr(OV._streamer, "ensure_running", lambda: None)
    monkeypatch.setattr(OV._desktop_feed, "ensure_running", lambda s: None)
    a = Flask(__name__)
    a.register_blueprint(OV.bp)
    return a


@pytest.mark.parametrize("route,fn", [
    ("/operator/frame", "operator_frame"),
    ("/operator/stream", "operator_stream"),
])
@pytest.mark.parametrize("qs,expect", [
    ("?tier=lo", "lo"), ("?tier=hi", "hi"), ("?tier=bogus", "hi"), ("", "hi"),
])
def test_feed_routes_set_tier_on_both_sources(app, route, fn, qs, expect):
    OV._streamer.tier = "x"          # sentinel: the route must overwrite it
    OV._desktop_feed.tier = "x"
    with app.test_request_context(route + qs):
        getattr(OV, fn)()            # build the Response; never iterate the stream
    assert OV._streamer.tier == expect
    assert OV._desktop_feed.tier == expect


# ── F1: sandbox stream honors the tier ───────────────────────────────────────

class _FakeSandbox:
    """Stands in for sandbox_container: records open_stream args, feeds frames."""
    def __init__(self, chunks):
        self.opened = []
        self.stopped = 0
        self._chunks = list(chunks)
        fake = self

        class _Out:
            def read1(self, n):
                return fake._chunks.pop(0) if fake._chunks else b""

        class _Proc:
            stdout = _Out()

        self._proc = _Proc()

    def open_stream(self, fps=10, quality=8):
        self.opened.append((fps, quality))
        return self._proc

    def stop_stream(self, proc):
        self.stopped += 1

    def split_jpegs(self, buf):
        return ([buf], b"") if buf else ([], b"")


def _fresh_feed(sb):
    feed = OV._DesktopFeed()
    feed._mods["sandbox"] = sb
    feed._running = True
    feed.surface = "desktop-sandbox"
    feed.last_view = time.monotonic()
    return feed


def test_sandbox_stream_spawns_with_lo_tier_params():
    sb = _FakeSandbox([])
    feed = _fresh_feed(sb)
    feed.tier = "lo"
    assert feed._stream() is True
    assert sb.opened == [(OV.TIER_LO_SANDBOX_FPS, OV.TIER_LO_SANDBOX_Q)]
    assert sb.stopped == 1


def test_sandbox_stream_spawns_with_hi_tier_defaults():
    sb = _FakeSandbox([])
    feed = _fresh_feed(sb)
    feed.tier = "hi"
    feed._stream()
    assert sb.opened == [(10, 8)]


def test_sandbox_stream_exits_on_tier_change_to_respawn():
    """A tier flip mid-stream must end the read loop so the outer loop
    respawns ffmpeg with the new rate/quality."""
    sb = _FakeSandbox([b"\xff\xd8frame1", b"\xff\xd8frame2", b"\xff\xd8frame3"])
    feed = _fresh_feed(sb)
    feed.tier = "hi"

    real_split = sb.split_jpegs

    def split_and_flip(buf):
        feed.tier = "lo"              # viewer switched tiers mid-read
        return real_split(buf)

    sb.split_jpegs = split_and_flip
    assert feed._stream() is True
    assert sb._chunks, "read loop kept draining after the tier changed"


# ── F2: eager post-action frames, idle cadence unchanged ─────────────────────

def test_streamer_pace_sleeps_full_interval_when_idle():
    st = OV._Streamer()
    t0 = time.monotonic()
    asyncio.run(st._pace(0.15))
    assert time.monotonic() - t0 >= 0.14   # no idle-cadence regression


def test_streamer_pace_wakes_early_on_eager_event():
    st = OV._Streamer()

    async def run():
        st._eager_evt = asyncio.Event()

        async def poke():
            await asyncio.sleep(0.02)
            st._eager_evt.set()

        asyncio.ensure_future(poke())
        t0 = time.monotonic()
        await st._pace(1.0)
        return time.monotonic() - t0

    elapsed = asyncio.run(run())
    assert elapsed < 0.5, f"eager event did not wake the pace ({elapsed:.2f}s)"


def test_streamer_pace_clears_event_after_wake():
    st = OV._Streamer()

    async def run():
        st._eager_evt = asyncio.Event()
        st._eager_evt.set()
        await st._pace(1.0)
        return st._eager_evt.is_set()

    assert asyncio.run(run()) is False   # consume-once: next pace is normal


def test_do_action_sets_the_eager_event():
    st = OV._Streamer()
    st._page = object()                  # past the no-page guard

    async def run():
        res = await st._do_action({"kind": "definitely-not-a-kind"})
        return res, st._eager_evt.is_set()

    res, eager = asyncio.run(run())
    assert not res["ok"]                 # unknown kind still errors like before
    assert eager is True                 # ...but the next grab is eager


def test_do_action_without_page_does_not_poke():
    st = OV._Streamer()
    st._page = None
    res = asyncio.run(st._do_action({"kind": "click"}))
    assert not res["ok"]
    assert st._eager_evt is None or not st._eager_evt.is_set()


def test_desktop_pace_sleeps_full_interval_when_idle():
    feed = OV._DesktopFeed()
    t0 = time.monotonic()
    feed._pace(0.15)
    assert time.monotonic() - t0 >= 0.14


def test_desktop_pace_wakes_early_on_poke():
    feed = OV._DesktopFeed()
    threading.Timer(0.02, feed._wake.set).start()
    t0 = time.monotonic()
    feed._pace(1.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"poke did not wake the desktop pace ({elapsed:.2f}s)"
    assert not feed._wake.is_set()       # consume-once


def test_desktop_steer_pokes_the_feed(monkeypatch):
    calls = []

    class _FakeMod:
        @staticmethod
        def size():
            return (1024, 768)

        @staticmethod
        def execute(a, *rest):
            calls.append(a)

    monkeypatch.setitem(OV._active_surface, "name", "desktop-sandbox")
    monkeypatch.setattr(OV, "_load_cu", lambda name: _FakeMod)
    OV._desktop_feed._wake.clear()
    res = OV._desktop_steer({"kind": "click_at", "x": 0.5, "y": 0.5})
    assert res["ok"] and calls
    assert OV._desktop_feed._wake.is_set()
