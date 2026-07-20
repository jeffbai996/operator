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
    """Records CDP sends; answers captureScreenshot with a tiny valid payload
    and getLayoutMetrics with the given metrics (raises when None — the
    'metrics unavailable' path)."""
    def __init__(self, metrics=None):
        self.calls = []
        self._metrics = metrics

    async def send(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"\xff\xd8fakejpeg").decode()}
        if method == "Page.getLayoutMetrics":
            if self._metrics is None:
                raise RuntimeError("no metrics")
            return self._metrics
        return {"result": {"value": "null"}}


def _mk_metrics(cw, ch, zoom=1.1, page_x=0, page_y=0):
    """Device viewport = css × zoom — the Windows display-scale shape that
    produced the cropped/offset frames (2026-07-12)."""
    return {"layoutViewport": {"pageX": page_x * zoom, "pageY": page_y * zoom,
                               "clientWidth": cw * zoom, "clientHeight": ch * zoom},
            "cssLayoutViewport": {"pageX": page_x, "pageY": page_y,
                                  "clientWidth": cw, "clientHeight": ch}}


def _grab_with(tier, vw=1400, vh=900, metrics="auto"):
    st = OV._Streamer()
    st.tier = tier
    st.vw, st.vh = vw, vh
    if metrics == "auto":
        metrics = _mk_metrics(vw, vh)
    # _grab sessions are identity-checked against the page (Codex P1: a cache
    # bound to another tab streamed the wrong page) — seed _cdp_for to match.
    pg = object()
    sess = _FakeSess(metrics)
    st._cdp = sess
    st._cdp_for = pg
    out = asyncio.run(st._grab(pg))
    shot = next(p for m, p in sess.calls if m == "Page.captureScreenshot")
    return out, shot


def test_grab_hi_clips_full_device_viewport_at_native_res():
    """2026-07-12 rev 2: clip covers the FULL device viewport (no right/bottom
    crop — the owner "right edge cut off") and outputs at DEVICE resolution
    (scale=1.0). Click accuracy is independent of frame size — the frontend
    sends normalized (0..1) coords that _viewport_css maps to CSS px — so we
    keep native sharpness instead of downscaling to CSS width. Downscaling only
    the CDP path (while the fallback captured device-res) flipped served frame
    sizes 690<->863 mid-nav → the phone rescaled each swap ("spasming small/big
    at constant frequency") and the small frames upscaled soft ("pixelated")."""
    out, shot = _grab_with("hi")
    assert out == b"\xff\xd8fakejpeg"
    assert shot["quality"] == OV.JPEG_QUALITY
    clip = shot["clip"]
    assert clip["width"] == pytest.approx(1400 * 1.1)   # full device viewport
    assert clip["height"] == pytest.approx(900 * 1.1)
    assert clip["scale"] == pytest.approx(1.0)          # native device res


def test_grab_clip_follows_scrolled_device_viewport():
    """The clip is in document coordinates. Anchoring it at (0, 0) while the
    page is scrolled captures the area above the viewport, which produced a
    giant blank band over Yahoo Finance on the scaled Windows Chrome."""
    metrics = _mk_metrics(1098, 980, zoom=1.25, page_x=8, page_y=475.2)
    _, shot = _grab_with("hi", vw=1098, vh=980, metrics=metrics)
    clip = shot["clip"]
    assert clip["x"] == pytest.approx(10)
    assert clip["y"] == pytest.approx(594)


def test_grab_lo_downscales_and_compresses_harder():
    _, shot = _grab_with("lo", vw=1400, vh=900)
    assert shot["quality"] == OV.TIER_LO_QUALITY < OV.JPEG_QUALITY
    clip = shot["clip"]
    assert clip["width"] == pytest.approx(1400 * 1.1)   # still full-coverage clip
    # lo caps the output width at TIER_LO_MAX_W of the DEVICE width (fixed cap →
    # stable frame size, no rescale pulse).
    assert clip["scale"] == pytest.approx(OV.TIER_LO_MAX_W / (1400 * 1.1))


def test_grab_skips_clip_when_metrics_unavailable():
    _, shot = _grab_with("lo", vw=0, vh=0, metrics=None)
    assert shot["quality"] == OV.TIER_LO_QUALITY
    assert "clip" not in shot     # no metrics — unclipped full frame beats a blind crop


def test_grab_lo_narrow_device_viewport_not_upscaled():
    """When the device viewport is already under the lo cap, scale is clamped to
    1.0 — a lo frame is never UPSCALED past native (that would waste bytes and
    reintroduce the soft-upscale look)."""
    # device width = 800 * 1.1 = 880 < TIER_LO_MAX_W (900) → clamp to 1.0
    _, shot = _grab_with("lo", vw=800, vh=600)
    clip = shot["clip"]
    assert clip["scale"] == pytest.approx(1.0)


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
            st.view_w, st.view_h = 1400, 900
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


class _WheelSpyPage:
    """Fake page recording whether Playwright's high-level mouse.wheel is called
    — it must NOT be: on a connect_over_cdp handle it silently no-ops. Scroll
    must go through _cdp_scroll (raw CDP mouseWheel) instead."""
    class _Mouse:
        def __init__(self): self.wheel_calls = []
        async def wheel(self, dx, dy): self.wheel_calls.append((dx, dy))
    def __init__(self): self.mouse = self._Mouse()
    @property
    def url(self): return "https://example.test/"


def _scroll_dispatch(action):
    """Run _do_action(scroll) with _cdp_scroll stubbed to record its args and
    p.mouse.wheel spied. Returns (cdp_scroll_calls, mouse_wheel_calls)."""
    st = OV._Streamer()
    pg = _WheelSpyPage()
    st._page = pg
    cdp_calls = []

    async def fake_cdp_scroll(p, dx, dy):
        cdp_calls.append((dx, dy))
    st._cdp_scroll = fake_cdp_scroll

    async def run():
        return await st._do_action(action)
    asyncio.run(run())
    return cdp_calls, pg.mouse.wheel_calls


def test_scroll_numeric_uses_raw_cdp_not_playwright_wheel():
    """Regression: 'scroll up/down randomly broke' — p.mouse.wheel() no-ops on
    the CDP handle. Numeric dy must dispatch via _cdp_scroll, never mouse.wheel."""
    cdp, wheel = _scroll_dispatch({"kind": "scroll", "dx": 0, "dy": 500})
    assert cdp == [(0.0, 500.0)]
    assert wheel == [], "scroll must NOT use Playwright p.mouse.wheel (it no-ops)"


def test_scroll_keyword_uses_raw_cdp():
    """Keyword scrolls (up/down/top/bottom) also route through _cdp_scroll."""
    cdp, wheel = _scroll_dispatch({"kind": "scroll", "value": "down",
                                   "dx": None, "dy": None})
    assert cdp == [(0, 600)]          # 'down' → +600
    assert wheel == []
    cdp, _ = _scroll_dispatch({"kind": "scroll", "value": "bottom",
                               "dx": None, "dy": None})
    assert cdp == [(0, 100000)]       # 'bottom' → large positive delta


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
