"""Streamer driver lifecycle — the 2026-07-10 incident class.

That night: `_attach()` started a playwright driver process, then
`connect_over_cdp` failed, the exception propagated out of `_grab_loop`, and
NOTHING stopped the driver — while `ensure_running()` relaunched the whole
thread on every /frame poll with no backoff. Result: 464 errors/hr and 15
orphaned node driver processes.

These tests pin the fixed contract:
  * the driver is stopped on EVERY exit path, including attach failure,
  * an abnormal exit arms a relaunch backoff that ensure_running() honors,
  * the backoff grows with the failure streak and caps,
  * a successful frame grab resets the streak (recovery is fast again),
  * the attach fallback page is navigated to the landing URL, not left
    on about:blank,
  * _teardown() preserves an error status (the wedge/attach error message
    must survive teardown so the UI can show it).

All playwright interaction is faked via sys.modules — no real browser, no
network, runs in any venv.
"""
import asyncio
import base64
import sys
import time
import types

import pytest

import operator_view as OV


# ---------------------------------------------------------------- fakes ----

_JPEG_B64 = base64.b64encode(OV._PLACEHOLDER_JPEG).decode()


class FakeSess:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Page.captureScreenshot":
            return {"data": _JPEG_B64}
        if method == "Page.getLayoutMetrics":
            return {"visualViewport": {"clientWidth": 1280, "clientHeight": 800}}
        return {}

    async def detach(self):
        pass


class FakePage:
    def __init__(self, ctx):
        self._ctx = ctx
        self.url = "about:blank"

    def is_closed(self):
        return False

    @property
    def context(self):
        return self._ctx

    @property
    def viewport_size(self):
        return {"width": 1280, "height": 800}

    async def evaluate(self, expr, *args):
        if "innerWidth" in expr:
            return {"w": 1280, "h": 800}
        if "visibilityState" in expr:
            return "visible"
        return None

    async def bring_to_front(self):
        pass

    async def title(self):
        return "fake"


class FakeCtx:
    def __init__(self, n_pages=1):
        self.pages = [FakePage(self) for _ in range(n_pages)]
        self.sess = FakeSess()
        self.new_pages = 0

    async def new_cdp_session(self, page):
        return self.sess

    async def add_init_script(self, script):
        pass

    async def new_page(self):
        self.new_pages += 1
        p = FakePage(self)
        self.pages.append(p)
        return p


class FakeBrowser:
    def __init__(self, ctx):
        self.contexts = [ctx]
        self.closed = 0

    async def close(self):
        self.closed += 1


class FakePW:
    """Stands in for the STARTED playwright driver (what .start() returns)."""

    def __init__(self, ctx=None, connect_exc=None):
        self.stopped = 0
        self._ctx = ctx
        self._exc = connect_exc
        self.chromium = self

    async def connect_over_cdp(self, url):
        if self._exc is not None:
            raise self._exc
        return FakeBrowser(self._ctx)

    async def stop(self):
        self.stopped += 1


def _install(monkeypatch, pws: list):
    """Fake playwright.async_api; each async_playwright() start pops from pws
    (mirrors 'a new driver process per start')."""
    created = []

    class _AP:
        async def start(self):
            pw = pws[len(created)] if len(created) < len(pws) else pws[-1]
            created.append(pw)
            return pw

    mod = types.ModuleType("playwright.async_api")
    mod.async_playwright = lambda: _AP()
    pkg = types.ModuleType("playwright")
    pkg.async_api = mod
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", mod)
    return created


@pytest.fixture()
def streamer(monkeypatch):
    # never touch a real browser: connect is faked, and the CDP liveness probe
    # is stubbed out — on WSL mirrored networking a urlopen to a dead loopback
    # port hangs the full 3s timeout instead of RSTing, which eats the test's
    # idle window and skews timing assertions. The probe isn't under test here.
    monkeypatch.setattr(OV, "CDP_URL", "http://127.0.0.1:9299")
    monkeypatch.setattr(OV._Streamer, "_ensure_chrome_alive", lambda self: None)
    s = OV._Streamer()
    s.last_view = time.monotonic()
    return s


# ---------------------------------------------------------------- tests ----

def test_driver_stopped_when_connect_fails(monkeypatch, streamer):
    pw = FakePW(connect_exc=ConnectionError("no chrome"))
    _install(monkeypatch, [pw])
    streamer._running = True
    streamer._run()
    assert pw.stopped == 1, "driver must be stopped when connect_over_cdp fails"
    assert streamer.status == "error"
    assert not streamer._running


def test_no_driver_leak_across_repeated_failed_runs(monkeypatch, streamer):
    pws = [FakePW(connect_exc=ConnectionError("down")) for _ in range(3)]
    created = _install(monkeypatch, pws)
    for _ in range(3):
        streamer._backoff_until = 0.0     # test isolates the LEAK, not the pacing
        streamer._running = True
        streamer._run()
    assert len(created) == 3
    assert all(pw.stopped == 1 for pw in pws), \
        f"every started driver must be stopped: {[p.stopped for p in pws]}"


def test_error_exit_arms_backoff_and_ensure_running_respects_it(monkeypatch, streamer):
    pw = FakePW(connect_exc=ConnectionError("down"))
    _install(monkeypatch, [pw])
    streamer._running = True
    streamer._run()
    assert streamer._fail_streak == 1
    assert streamer._backoff_until > time.monotonic(), "error exit must arm backoff"

    # within the backoff window ensure_running must NOT spawn a thread
    streamer._thread = None
    streamer.ensure_running()
    assert streamer._thread is None, "relaunch during backoff window"

    # window elapsed → relaunch is allowed again (thread spawns, fails, dies)
    streamer._backoff_until = time.monotonic() - 0.01
    streamer.ensure_running()
    assert streamer._thread is not None
    streamer._thread.join(timeout=5)
    assert streamer._fail_streak == 2, "second failure must grow the streak"


def test_backoff_grows_and_caps(monkeypatch, streamer):
    pw = FakePW(connect_exc=ConnectionError("down"))
    _install(monkeypatch, [pw])
    streamer._fail_streak = 9            # deep into a bad night
    streamer._running = True
    streamer._run()
    delay = streamer._backoff_until - time.monotonic()   # relative to arm time
    assert delay <= 10.5, f"backoff must cap (~10s), got {delay:.1f}s"
    assert delay >= 5.0, f"deep-streak backoff should be near the cap, got {delay:.1f}s"


def test_full_run_stops_driver_and_resets_streak(monkeypatch, streamer):
    """Healthy attach + a few grabbed frames + idle exit: driver stopped once,
    fail streak cleared by the first good frame, no stray new pages."""
    ctx = FakeCtx(n_pages=1)
    pw = FakePW(ctx=ctx)
    _install(monkeypatch, [pw])
    monkeypatch.setattr(OV, "IDLE_STOP_AFTER", 0.35)
    streamer._fail_streak = 3
    streamer._backoff_until = 0.0
    streamer._running = True
    streamer._run()
    assert pw.stopped == 1
    assert streamer._fail_streak == 0, "a good frame must reset the fail streak"
    assert streamer._backoff_until == 0.0
    assert any(m == "Page.captureScreenshot" for m, _ in ctx.sess.sent)
    assert ctx.new_pages == 0
    assert streamer.status == "idle"
    assert streamer.frame is None        # cleared on stop — no stale 'live' frame


def test_attach_fallback_page_leaves_about_blank(monkeypatch, streamer):
    """No open pages at attach → the fallback page must be navigated to the
    landing URL (a bare ctx.new_page() sits on about:blank forever)."""
    ctx = FakeCtx(n_pages=0)
    pw = FakePW(ctx=ctx)
    _install(monkeypatch, [pw])
    asyncio.run(streamer._attach())
    assert ctx.new_pages == 1
    navs = [p for m, p in ctx.sess.sent if m == "Page.navigate"]
    assert navs and navs[0]["url"] == OV._NEWTAB_DATA_URL, \
        f"fallback page must be navigated to the landing URL: {ctx.sess.sent}"
    assert streamer.status == "live"


def test_teardown_preserves_error_status(streamer):
    streamer.status, streamer.detail = "error", "Chrome wedged"
    asyncio.run(streamer._teardown())
    assert streamer.status == "error", "teardown must not mask an error status"
    streamer.status = "live"
    asyncio.run(streamer._teardown())
    assert streamer.status == "idle"


# ------------------------------------------- emulation hygiene (zoom spaz) --

class _RaisingCtx(FakeCtx):
    """new_cdp_session raises for the LAST page — per-page failures must not
    abort the sweep."""

    async def new_cdp_session(self, page):
        if page is self.pages[-1]:
            raise RuntimeError("target crashed")
        return self.sess


def test_clear_emulation_sweeps_every_page(streamer):
    ctx = _RaisingCtx(n_pages=3)
    streamer._browser = FakeBrowser(ctx)
    res = asyncio.run(streamer._clear_emulation())
    assert res["ok"] and res["cleared"] == 2 and res["failed"] == 1
    metrics = [m for m, _ in ctx.sess.sent if m == "Emulation.clearDeviceMetricsOverride"]
    touch = [p for m, p in ctx.sess.sent if m == "Emulation.setTouchEmulationEnabled"]
    assert len(metrics) == 2, "device-metrics override cleared per reachable page"
    assert touch and all(p == {"enabled": False} for p in touch), \
        "touch emulation must be switched OFF (it kills wheel scrolling)"


def test_clear_emulation_without_browser_is_safe(streamer):
    streamer._browser = None
    res = asyncio.run(streamer._clear_emulation())
    assert res["ok"] is False and res["cleared"] == 0


def test_agent_run_falling_edge_triggers_one_sweep(monkeypatch, streamer):
    """While an agent runs the streamer must NOT touch emulation (it would
    fight a run that resized deliberately); the moment the run ends, exactly
    one sweep fires."""
    ctx = FakeCtx(n_pages=1)
    pw = FakePW(ctx=ctx)
    _install(monkeypatch, [pw])
    monkeypatch.setattr(OV, "IDLE_STOP_AFTER", 30.0)
    monkeypatch.setattr(OV, "FRAME_INTERVAL", 0.01)

    sweeps = []

    async def _fake_sweep():
        sweeps.append(time.monotonic())
        return {"ok": True, "cleared": 1, "failed": 0}
    monkeypatch.setattr(streamer, "_clear_emulation", _fake_sweep)

    # busy for 2 polls, then idle; stop the loop a few iterations later
    seq = iter([True, True, False, False, False])

    class _Runner:
        def is_running(self):
            v = next(seq, None)
            if v is None:
                streamer._running = False
                return False
            return v
    monkeypatch.setattr(OV.operator_agent, "runner", _Runner())

    streamer._running = True
    streamer._run()
    assert len(sweeps) == 1, f"exactly one sweep on run end, got {len(sweeps)}"


def test_reset_view_steer_action(streamer):
    """kind=reset_view reaches the sweep through the normal steer path, so the
    cockpit menu AND curl both use it."""
    ctx = FakeCtx(n_pages=2)
    streamer._browser = FakeBrowser(ctx)
    streamer._page = ctx.pages[0]
    res = asyncio.run(streamer._do_action({"kind": "reset_view"}))
    assert res["ok"] is True and res["cleared"] == 2
    assert any(m == "Emulation.clearDeviceMetricsOverride" for m, _ in ctx.sess.sent)
