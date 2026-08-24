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
from pathlib import Path

import pytest

import operator_view as OV


# ---------------------------------------------------------------- fakes ----

_JPEG_B64 = base64.b64encode(OV._PLACEHOLDER_JPEG).decode()


class FakeSess:
    def __init__(self):
        self.sent = []
        self.detached = 0

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Page.captureScreenshot":
            return {"data": _JPEG_B64}
        if method == "Page.getLayoutMetrics":
            return {"visualViewport": {"clientWidth": 1280, "clientHeight": 800}}
        return {}

    async def detach(self):
        self.detached += 1


class FakePage:
    def __init__(self, ctx):
        self._ctx = ctx
        self.url = "about:blank"
        self.evaluated = []
        self._closed = False
        self.fronted = 0

    def is_closed(self):
        return self._closed

    @property
    def context(self):
        return self._ctx

    @property
    def viewport_size(self):
        return {"width": 1280, "height": 800}

    async def evaluate(self, expr, *args):
        self.evaluated.append((expr, args))
        if "innerWidth" in expr:
            return {"w": 1280, "h": 800}
        if "visibilityState" in expr:
            return "visible"
        return None

    async def bring_to_front(self):
        self.fronted += 1

    async def close(self):
        self._closed = True

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


def test_first_viewer_demand_starts_closed_chrome(monkeypatch):
    """A cold Operator attach launches Chrome once, then attaches normally."""
    s = OV._Streamer()
    probes = iter([False, True])
    launches = []
    monkeypatch.setattr(s, "_cdp_alive", lambda: next(probes))
    monkeypatch.setattr(
        s, "_launch_chrome",
        lambda: launches.append(OV._Streamer._chrome_attach_script()),
    )

    s._ensure_chrome_alive()

    assert launches == [OV._Streamer._chrome_attach_script()]
    assert s._user_closed is False


def test_chrome_attach_script_honors_launcher_override(monkeypatch):
    """operator-fam's :9333 Windows profile uses its own scheduled-task
    launcher; the standalone override must win over every default."""
    monkeypatch.setenv("OPERATOR_CHROME_LAUNCHER", "~/local-projects/operator-fam/opfam-chrome.sh")
    import os
    assert OV._Streamer._chrome_attach_script() == os.path.expanduser(
        "~/local-projects/operator-fam/opfam-chrome.sh")


def test_chrome_attach_script_falls_back_without_override(monkeypatch):
    monkeypatch.delenv("OPERATOR_CHROME_LAUNCHER", raising=False)
    import os
    expected = (
        "~/local-projects/operator-demo/op-demo-chrome.sh" if OV.DEMO
        else "~/agents/browse/chrome-attach.sh"
    )
    assert OV._Streamer._chrome_attach_script() == os.path.expanduser(expected)


def test_closed_chrome_stays_closed_without_operator_demand(monkeypatch):
    """Constructing the module streamer/server must not eagerly launch Chrome."""
    launches = []
    monkeypatch.setattr(OV._Streamer, "_launch_chrome",
                        lambda self: launches.append("launched"))

    OV._Streamer()

    assert launches == []
    source = Path(OV.__file__).read_text(encoding="utf-8")
    assert "_launch_chrome_on_boot" not in source
    assert "operator-chrome-boot" not in source


def test_failed_demand_start_aborts_before_playwright(monkeypatch):
    """If the launcher cannot restore CDP, fail clearly instead of leaking a
    Playwright driver that can never attach."""
    s = OV._Streamer()
    monkeypatch.setattr(s, "_cdp_alive", lambda: False)
    monkeypatch.setattr(s, "_launch_chrome", lambda: None)

    with pytest.raises(ConnectionError, match="could not start"):
        s._ensure_chrome_alive()

    assert s.status == "error"
    assert s._user_closed is True


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


def test_attach_existing_blank_page_lands_on_home(monkeypatch, streamer):
    """A freshly launched Chrome already has ONE tab — its own default
    new-tab page — so `ctx.pages` is non-empty and the code takes the
    'reuse existing pages' branch, never reaching the no-pages fallback
    above. That existing page sits on about:blank and nothing navigates
    it (the owner 2026-08-04, reproduced live: 'right now it lands in
    about:blank' after auto-heal relaunches Chrome). The fix must treat a
    blank EXISTING page the same as having no page at all: land it on the
    same landing URL as the true no-pages case, not leave it inert."""
    ctx = FakeCtx(n_pages=1)          # FakePage defaults .url = "about:blank"
    pw = FakePW(ctx=ctx)
    _install(monkeypatch, [pw])
    asyncio.run(streamer._attach())
    assert ctx.new_pages == 0, "must reuse the existing tab, not open a second one"
    navs = [p for m, p in ctx.sess.sent if m == "Page.navigate"]
    assert navs and navs[0]["url"] == OV._NEWTAB_DATA_URL, \
        f"the sole existing page is blank and must be navigated home: {ctx.sess.sent}"


def test_attach_existing_real_page_is_left_alone(monkeypatch, streamer):
    """The counterpart to the test above: a page that's actually showing
    something must NEVER be redirected out from under the user on attach."""
    ctx = FakeCtx(n_pages=1)
    ctx.pages[0].url = "https://example.com/dashboard"
    pw = FakePW(ctx=ctx)
    _install(monkeypatch, [pw])
    asyncio.run(streamer._attach())
    navs = [p for m, p in ctx.sess.sent if m == "Page.navigate"]
    assert not navs, f"a real page must not be navigated away on attach: {navs}"


def test_attach_prefers_restored_real_page_over_launcher_blank(monkeypatch, streamer):
    """A persistent profile can restore useful tabs while Chrome also creates
    a synthetic about:blank launcher tab. The cockpit must show the restored
    work, not promote the synthetic blank and stream a black rectangle."""
    ctx = FakeCtx(n_pages=3)
    ctx.pages[1].url = "https://example.com/dashboard"
    ctx.pages[2].url = "https://example.com/reports"
    pw = FakePW(ctx=ctx)
    _install(monkeypatch, [pw])

    asyncio.run(streamer._attach())

    assert streamer._page is ctx.pages[1]
    assert streamer._page.fronted == 1
    navs = [p for m, p in ctx.sess.sent if m == "Page.navigate"]
    assert not navs, "restored pages must be preserved, not replaced with home"


def test_attach_forces_desktop_identity(monkeypatch, streamer):
    ctx = FakeCtx(n_pages=1)
    pw = FakePW(ctx=ctx)
    _install(monkeypatch, [pw])

    asyncio.run(streamer._attach())

    calls = [p for m, p in ctx.sess.sent if m == "Emulation.setUserAgentOverride"]
    assert calls
    assert "Windows NT 10.0; Win64; x64" in calls[0]["userAgent"]
    assert calls[0]["platform"] == "Win32"
    assert calls[0]["userAgentMetadata"]["mobile"] is False
    assert calls[0]["userAgentMetadata"]["platform"] == "Windows"
    # force-desktop now OVERWRITES stale metrics (no clear-first — the
    # clear+apply pair was one visible size pulse per nav, the 2026-07-26
    # strobe); the apply itself is the desktop-identity marker.
    assert any(m == "Emulation.setDeviceMetricsOverride" for m, _ in ctx.sess.sent)
    assert any(m == "Emulation.setTouchEmulationEnabled" and p == {"enabled": False}
               for m, p in ctx.sess.sent)


def test_attach_forces_phone_legible_view_width(monkeypatch, streamer):
    # Attach must reflow the shared Chrome to view_w via device metrics — this is
    # what fixes "everything miniscule" (the layout canvas was stuck ~1349px).
    ctx = FakeCtx(n_pages=1)
    pw = FakePW(ctx=ctx)
    _install(monkeypatch, [pw])

    asyncio.run(streamer._attach())

    metrics = [params for method, params in ctx.sess.sent
               if method == "Emulation.setDeviceMetricsOverride"]
    assert metrics, "attach must apply a device-metrics viewport override"
    assert metrics[-1]["width"] == streamer.view_w
    assert metrics[-1]["mobile"] is False  # desktop layout, just narrower


def test_attach_applies_persisted_nondefault_zoom_to_current_page(monkeypatch, streamer):
    streamer.zoom = 0.7  # non-default → must be re-applied on attach
    ctx = FakeCtx(n_pages=1)
    pw = FakePW(ctx=ctx)
    _install(monkeypatch, [pw])

    asyncio.run(streamer._attach())

    assert any("document.documentElement.style.zoom" in expr and args == (0.7,)
               for expr, args in ctx.pages[0].evaluated)


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


def test_clear_emulation_keeps_active_metrics_session_attached(streamer):
    ctx = FakeCtx(n_pages=1)
    streamer._browser = FakeBrowser(ctx)
    streamer._page = ctx.pages[0]

    res = asyncio.run(streamer._clear_emulation())

    assert res["ok"] is True
    assert streamer._metric_sessions[streamer._page] is ctx.sess
    assert ctx.sess.detached == 0
    assert any(method == "Emulation.setDeviceMetricsOverride"
               for method, _ in ctx.sess.sent)


def test_capture_and_metrics_share_the_same_persistent_target_session(streamer):
    class MultiSessionCtx(FakeCtx):
        def __init__(self):
            super().__init__(n_pages=1)
            self.sessions = []

        async def new_cdp_session(self, page):
            sess = FakeSess()
            self.sessions.append(sess)
            return sess

    ctx = MultiSessionCtx()
    streamer._browser = FakeBrowser(ctx)
    streamer._page = ctx.pages[0]

    async def bind_sessions():
        await streamer._force_desktop_page(streamer._page)
        return (await streamer._metric_session(streamer._page),
                await streamer._cdp_session(streamer._page))

    metrics, capture = asyncio.run(bind_sessions())

    assert capture is metrics
    assert len(ctx.sessions) == 1
    assert any(method == "Emulation.setDeviceMetricsOverride"
               for method, _ in capture.sent)


def test_new_tab_click_activates_the_shadow_dom_control(streamer):
    """Chrome's privileged NTP ignores otherwise valid CDP mouse clicks."""
    class NewTabSess(FakeSess):
        def __init__(self):
            super().__init__()
            self.activated = False

        async def send(self, method, params=None):
            self.sent.append((method, params))
            if method == "Page.getLayoutMetrics":
                return {"cssLayoutViewport": {
                    "clientWidth": 1024, "clientHeight": 800}}
            if method == "Runtime.evaluate":
                expr = (params or {}).get("expression", "")
                if "__opSelectOverlay" in expr:
                    return {"result": {"value": False}}
                self.activated = True
                return {"result": {"value": True}}
            return {}

    ctx = FakeCtx(n_pages=1)
    ctx.sess = NewTabSess()
    page = ctx.pages[0]
    page.url = "chrome://new-tab-page/"
    streamer._browser = FakeBrowser(ctx)
    streamer._page = page

    result = asyncio.run(streamer._do_action({
        "kind": "click_at", "x": 0.2, "y": 0.3, "count": 1}))

    assert result["ok"] is True
    assert ctx.sess.activated is True
    assert not any(method == "Input.dispatchMouseEvent"
                   for method, _ in ctx.sess.sent)


def test_crashed_active_page_drops_stale_frame_and_uses_survivor(streamer):
    """A renderer crash may leave a Playwright page that is not `closed`."""
    ctx = FakeCtx(n_pages=2)
    survivor, crashed = ctx.pages
    streamer._browser = FakeBrowser(ctx)
    streamer._page = crashed
    streamer.frame = b"stale pixels"
    streamer.frame_ts = time.monotonic()
    streamer.status = "live"

    streamer._mark_page_crashed(crashed)
    asyncio.run(streamer._refresh_active_page())

    assert streamer._page is survivor
    assert streamer.frame is None
    assert streamer.status == "connecting"


def test_switch_tab_reapplies_desktop_metrics(streamer):
    ctx = FakeCtx(n_pages=2)
    streamer._browser = FakeBrowser(ctx)
    streamer._page = ctx.pages[0]

    res = asyncio.run(streamer._switch_tab(1))

    assert res["ok"] is True
    assert streamer._page is ctx.pages[1]
    assert any(method == "Emulation.setDeviceMetricsOverride"
               for method, _ in ctx.sess.sent)


def test_closing_active_tab_rebinds_survivor_at_desktop_width(streamer):
    """Open tab 2, close it, and tab 1 must never inherit phone metrics."""
    class PerPageCtx(FakeCtx):
        def __init__(self):
            super().__init__(n_pages=2)
            self.by_page = {page: FakeSess() for page in self.pages}

        async def new_cdp_session(self, page):
            return self.by_page[page]

    ctx = PerPageCtx()
    streamer._browser = FakeBrowser(ctx)
    streamer._page = ctx.pages[1]

    result = asyncio.run(streamer._close_tab(1))

    survivor = ctx.pages[0]
    calls = [params for method, params in ctx.by_page[survivor].sent
             if method == "Emulation.setDeviceMetricsOverride"]
    assert result["ok"] is True
    assert streamer._page is survivor and survivor.fronted == 1
    assert calls and calls[-1]["width"] >= 1024
    assert calls[-1]["mobile"] is False
    assert any(method == "Page.captureScreenshot"
               for method, _ in ctx.by_page[survivor].sent)


def test_capture_repairs_collapsed_viewport(streamer):
    class CollapsedSess(FakeSess):
        def __init__(self):
            super().__init__()
            self.repaired = False

        async def send(self, method, params=None):
            self.sent.append((method, params))
            if method == "Emulation.setDeviceMetricsOverride":
                self.repaired = True
                return {}
            if method == "Page.getLayoutMetrics":
                width = 1024 if self.repaired else 720
                return {"layoutViewport": {"clientWidth": width,
                                            "clientHeight": 690}}
            if method == "Page.captureScreenshot":
                return {"data": _JPEG_B64}
            return {}

    ctx = FakeCtx(n_pages=1)
    ctx.sess = CollapsedSess()
    streamer._browser = FakeBrowser(ctx)
    streamer._page = ctx.pages[0]
    streamer._cdp = ctx.sess
    streamer._cdp_for = streamer._page

    # The persistence gate (REPAIR_AFTER_MISSES) means a single failing frame
    # never repairs — a genuinely collapsed viewport fails every frame, so it
    # crosses the threshold and repairs within REPAIR_AFTER_MISSES grabs.
    frame = None
    for _ in range(OV.REPAIR_AFTER_MISSES):
        frame = asyncio.run(streamer._grab(streamer._page))

    assert frame
    assert streamer.vw >= 320
    assert any(method == "Emulation.setDeviceMetricsOverride"
               for method, _ in ctx.sess.sent)


def test_idle_stage_resize_publishes_a_fresh_frame_before_return(monkeypatch, streamer):
    """A completed resize action must not leave the old-layout frame buffered."""
    ctx = FakeCtx(n_pages=1)
    streamer._browser = FakeBrowser(ctx)
    streamer._page = ctx.pages[0]
    streamer.frame = b"old"
    monkeypatch.setattr(OV, "_VIEW_FOLLOW", True)
    monkeypatch.setattr(OV.operator_agent.runner, "is_running", lambda: False)

    async def apply_metrics(page, sess=None):
        streamer.vw, streamer.vh = streamer.view_w, streamer.view_h

    async def grab(page):
        return b"fresh"

    monkeypatch.setattr(streamer, "_apply_view_metrics", apply_metrics)
    monkeypatch.setattr(streamer, "_grab", grab)

    result = asyncio.run(streamer._do_action(
        {"kind": "stage_size", "value": "960x640"}))

    assert result["ok"] is True and result["view"] == [1280, 852]
    assert streamer.frame == b"fresh"
    assert streamer.frame_ts > 0


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


def test_ineffective_repairs_drop_session_then_go_dormant(streamer):
    """The 2026-07-23 'zooms in then back out, viewport static' pulse: the
    cached metric session was bound to a pre-navigation (frozen) target, so
    every repair's clear+apply perturbed the REAL page without ever changing
    the reading — a visible pulse every backoff period, forever. Now each dud
    repair drops the cached session (rebuild against the current target), and
    after 3 duds repairs go DORMANT until the gate recovers or the url flips."""

    class WedgedSess(FakeSess):
        async def send(self, method, params=None):
            self.sent.append((method, params))
            if method == "Page.captureScreenshot":
                return {"data": _JPEG_B64}
            if method == "Page.getLayoutMetrics":
                # under the floor, and NOTHING the repair does changes it
                return {"layoutViewport": {"clientWidth": 708,
                                           "clientHeight": 634}}
            return {}

    ctx = FakeCtx(n_pages=1)
    ctx.sess = WedgedSess()
    streamer._browser = FakeBrowser(ctx)
    streamer._page = ctx.pages[0]
    streamer._cdp = ctx.sess
    streamer._cdp_for = streamer._page
    streamer._repair_backoff = 0.0          # no waiting between repairs in test

    def repairs():
        # a repair reasserts metrics by OVERWRITE (clear-first was the strobe)
        return sum(1 for m, _ in ctx.sess.sent
                   if m == "Emulation.setDeviceMetricsOverride")

    # 3 dud repair rounds: each needs REPAIR_AFTER_MISSES misses to trigger
    for _ in range(3 * OV.REPAIR_AFTER_MISSES + 2):
        asyncio.run(streamer._grab(streamer._page))
        streamer._repair_ts = 0.0           # collapse the throttle window
    n_at_dormancy = repairs()
    assert n_at_dormancy == 3               # exactly the dud budget
    assert streamer._repair_dormant is True
    # every dud dropped + detached the cached session (the next _grab
    # legitimately rebuilds one — the drop forces a FRESH attach, that's the
    # point — so assert the detaches, not an empty map)
    assert ctx.sess.detached >= 3

    # dormant: more failing frames, ZERO further repairs (the pulse is gone)
    for _ in range(2 * OV.REPAIR_AFTER_MISSES):
        asyncio.run(streamer._grab(streamer._page))
        streamer._repair_ts = 0.0
    assert repairs() == n_at_dormancy

    # a navigation re-arms
    streamer._page.url = "https://example.test/next"
    asyncio.run(streamer._grab(streamer._page))
    assert streamer._repair_dormant is False


def test_walked_zone_never_repairs_only_collapse_band_does(streamer):
    """928x634 (a scrollbar-walked but perfectly usable page) sits between the
    960 gate floor and the 800 repair hard-floor: the gate misses but the
    clear+apply reflow — the user-visible 'zooms in then back out' pulse —
    must NOT fire. A genuinely collapsed ~700-wide emulation still repairs."""

    class WalkedSess(FakeSess):
        width = 928
        async def send(self, method, params=None):
            self.sent.append((method, params))
            if method == "Page.captureScreenshot":
                return {"data": _JPEG_B64}
            if method == "Page.getLayoutMetrics":
                return {"layoutViewport": {"clientWidth": self.width,
                                           "clientHeight": 634}}
            return {}

    ctx = FakeCtx(n_pages=1)
    ctx.sess = WalkedSess()
    streamer._browser = FakeBrowser(ctx)
    streamer._page = ctx.pages[0]
    streamer._cdp = ctx.sess
    streamer._cdp_for = streamer._page
    streamer._repair_backoff = 0.0

    def repairs():
        # a repair reasserts metrics by OVERWRITE (clear-first was the strobe)
        return sum(1 for m, _ in ctx.sess.sent
                   if m == "Emulation.setDeviceMetricsOverride")

    for _ in range(3 * OV.REPAIR_AFTER_MISSES):
        asyncio.run(streamer._grab(streamer._page))
        streamer._repair_ts = 0.0
    assert repairs() == 0                      # walked zone: no pulse, ever

    ctx.sess.width = 700                       # collapse band → repair engages
    for _ in range(OV.REPAIR_AFTER_MISSES + 1):
        asyncio.run(streamer._grab(streamer._page))
        streamer._repair_ts = 0.0
    assert repairs() >= 1
    assert OV.REPAIR_HARD_FLOOR_W == 800


def test_agent_run_rising_edge_reasserts_clobbered_view_metrics(monkeypatch, streamer):
    """Run START: the agent's Playwright MCP attaches to the shared Chrome and
    its emulation defaults drop our device-metrics override, so the canvas
    snaps back to the window's native width the instant a task begins (the owner
    2026-07-29).

    The repair loop cannot catch this — its gate only fires BELOW
    REPAIR_HARD_FLOOR_W and an attach makes the page WIDER — so the rising edge
    re-asserts instead. It must fire only while the live reading is off
    (_gate_misses > 0), and must stop once the viewport is ours again.
    """
    ctx = FakeCtx(n_pages=1)
    pw = FakePW(ctx=ctx)
    _install(monkeypatch, [pw])
    monkeypatch.setattr(OV, "IDLE_STOP_AFTER", 30.0)
    monkeypatch.setattr(OV, "FRAME_INTERVAL", 0.01)

    forced = []

    async def _fake_force(page, force=False):
        forced.append(force)
    monkeypatch.setattr(streamer, "_force_desktop_page", _fake_force)
    monkeypatch.setattr(streamer, "_clear_emulation",
                        lambda: asyncio.sleep(0, result={"ok": True}))

    # Viewport is clobbered for the whole window: the live reading never
    # matches view_w, which is what drives _gate_misses in _grab. (Presetting
    # _gate_misses does not work — _grab recomputes it every frame.)
    monkeypatch.setattr(streamer, "_matches_view_metrics", lambda w, h: False)

    seq = iter([False, True, True, True, False, False])

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

    # Assert on the flight-recorder, not on every _force_desktop_page call —
    # the grab path forces unrelatedly when it can't read a viewport.
    kinds = [e["kind"] for e in streamer._vp_events]
    assert "run-start-reassert" in kinds, f"rising edge must re-assert: {kinds}"
    assert any(f is True for f in forced), \
        f"re-assert is a one-shot boundary event, must bypass the storm guard: {forced}"


def test_run_start_reassert_stays_quiet_when_viewport_already_matches(
        monkeypatch, streamer):
    """A run that did NOT clobber the viewport must be left alone — otherwise
    we'd fight a run that resizes on purpose, which is exactly what the
    run-end-only design was protecting."""
    ctx = FakeCtx(n_pages=1)
    pw = FakePW(ctx=ctx)
    _install(monkeypatch, [pw])
    monkeypatch.setattr(OV, "IDLE_STOP_AFTER", 30.0)
    monkeypatch.setattr(OV, "FRAME_INTERVAL", 0.01)

    forced = []

    async def _fake_force(page, force=False):
        forced.append(force)
    monkeypatch.setattr(streamer, "_force_desktop_page", _fake_force)
    monkeypatch.setattr(streamer, "_clear_emulation",
                        lambda: asyncio.sleep(0, result={"ok": True}))

    # Reading matches view_w throughout, so the gate never registers a miss.
    monkeypatch.setattr(streamer, "_matches_view_metrics", lambda w, h: True)
    monkeypatch.setattr(streamer, "_accept_viewport", lambda w, h: True)

    seq = iter([False, True, True, True, False, False])

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

    kinds = [e["kind"] for e in streamer._vp_events]
    assert "run-start-reassert" not in kinds, \
        f"viewport already matches — must not touch emulation: {kinds}"


# ------------------------------------------------- active-tab following ----
# the owner 2026-07-29: "the issue where the operator browser doesn't focus on the
# tab that the bot is working on is still present" — after fixes on 07-08,
# 07-22 and 07-27. Root cause was a false premise, not a missing patch:
# _follow_active_tab decided foreground with document.visibilityState, and on
# the operator's CDP-driven Chrome EVERY tab answers 'visible' (measured: 4
# tabs, all 'visible', all hasFocus()==true, because an unfocused automation
# window never computes per-tab occlusion). So the `cur_vis == "visible"`
# early-return always matched and the view froze. Note FakePage.evaluate
# already returns "visible" unconditionally — the stub reproduces the real
# browser faithfully, which is why these tests are meaningful.
#
# The fix reads Chrome's own target list (/json/list, most-recently-activated
# first). These tests pin that behavior so the class can't come back.


def _follow_setup(monkeypatch, streamer, n_pages, active_index, busy=True):
    """Wire a streamer onto n fake tabs and declare which one is REALLY front."""
    ctx = FakeCtx(n_pages=n_pages)
    streamer._browser = FakeBrowser(ctx)
    for i, p in enumerate(ctx.pages):
        p.url = f"https://tab{i}.test/"
    streamer._page = ctx.pages[0]
    # stable synthetic target ids, and an active id pointing at active_index
    ids = {p: f"TARGET{i}" for i, p in enumerate(ctx.pages)}

    async def _pid(self, pg):
        return ids.get(pg)
    monkeypatch.setattr(OV._Streamer, "_page_target_id", _pid)
    monkeypatch.setattr(
        OV._Streamer, "_active_target_id",
        lambda self: (f"TARGET{active_index}" if active_index is not None else None))

    class _Runner:
        def is_running(self):
            return busy
    monkeypatch.setattr(OV.operator_agent, "runner", _Runner())
    monkeypatch.setattr(streamer, "_update_viewport", lambda: None)

    async def _force(pg):
        return None
    monkeypatch.setattr(streamer, "_force_desktop_page", _force)
    return ctx


def test_follows_the_real_front_tab_when_every_tab_claims_visible(
        monkeypatch, streamer):
    """The regression itself: same URLs, no navigation, agent on tab 2."""
    ctx = _follow_setup(monkeypatch, streamer, n_pages=3, active_index=2)
    # prime _tab_urls so the url-diff heuristic sees NO movement — the only
    # thing that can move the view here is the active-target check.
    streamer._tab_urls = {p: p.url for p in ctx.pages}
    asyncio.run(streamer._follow_active_tab())
    assert streamer._page is ctx.pages[2], (
        "must stream the tab Chrome reports as active; visibilityState says "
        "'visible' for all three so it cannot be the deciding signal")


def test_no_churn_when_already_on_the_front_tab(monkeypatch, streamer):
    """Idempotent: already correct → no page swap, no emulation reset."""
    ctx = _follow_setup(monkeypatch, streamer, n_pages=3, active_index=0)
    streamer._tab_urls = {p: p.url for p in ctx.pages}
    streamer._cdp = object()
    sentinel = streamer._cdp
    asyncio.run(streamer._follow_active_tab())
    assert streamer._page is ctx.pages[0]
    assert streamer._cdp is sentinel, "no-op check must not drop the CDP session"


def test_falls_back_to_visibility_when_target_list_unreachable(
        monkeypatch, streamer):
    """A wedged/unreachable CDP HTTP endpoint must not break following.

    active_index=None makes _active_target_id return None; behavior then
    reverts to the old visibility probe (a no-op where all tabs claim visible,
    which is exactly the pre-existing behavior — never worse)."""
    ctx = _follow_setup(monkeypatch, streamer, n_pages=2, active_index=None)
    streamer._tab_urls = {p: p.url for p in ctx.pages}
    asyncio.run(streamer._follow_active_tab())
    assert streamer._page is ctx.pages[0], "fallback path must not crash"


def test_target_id_cache_drops_closed_tabs(monkeypatch, streamer):
    """The id cache is bounded by LIVE tabs, not by every tab ever opened."""
    ctx = _follow_setup(monkeypatch, streamer, n_pages=2, active_index=0)
    streamer._tab_urls = {p: p.url for p in ctx.pages}
    # a page that is gone from ctx.pages still sitting in the cache
    ghost = FakePage(ctx)
    streamer._target_ids[ghost] = "TARGET_GHOST"
    for p in ctx.pages:
        streamer._target_ids[p] = "x"
    asyncio.run(streamer._follow_active_tab())
    assert ghost not in streamer._target_ids, \
        "closed-tab entries must be pruned so the cache can't grow unbounded"


# ---- dead-driver self-heal (the 2026-08-10 operator-fam incident) ----------
# The Playwright node driver died (uncaught CRPage frame-detach race on
# Google's cookie-rotation page). The old run_action posted coroutines onto
# the DEAD loop object left behind by the abnormal exit, so every click hung
# the full 30s and the cockpit read "browser disconnected" until a manual
# page refresh outlived the relaunch backoff.

def test_run_action_refuses_dead_loop_and_forces_reattach(streamer, monkeypatch):
    calls = []
    monkeypatch.setattr(streamer, "ensure_running", lambda: calls.append(1))
    # a loop that exists but is not running == the post-crash state
    dead = asyncio.new_event_loop()
    dead.close()
    streamer._loop = dead
    streamer._running = True
    streamer._backoff_until = time.monotonic() + 60   # deep in backoff
    out = streamer.run_action({"kind": "click_at", "x": 1, "y": 1})
    assert out["ok"] is False
    assert "reconnecting" in out["error"]
    # the force path must clear the backoff so the relaunch is immediate
    assert streamer._backoff_until == 0.0
    assert calls, "ensure_running was not invoked"


def test_run_action_driver_death_error_forces_reattach(streamer, monkeypatch):
    monkeypatch.setattr(streamer, "ensure_running", lambda: None)
    loop = asyncio.new_event_loop()
    import threading as _t
    t = _t.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        streamer._loop = loop
        streamer._running = True

        async def dies(action):
            raise RuntimeError("Target page, context or browser has been closed")
        monkeypatch.setattr(streamer, "_do_action", dies)
        streamer._backoff_until = time.monotonic() + 60
        out = streamer.run_action({"kind": "click_at", "x": 1, "y": 1})
        assert out["ok"] is False
        assert "reconnecting" in out["error"]
        assert streamer._backoff_until == 0.0
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


def test_run_action_ordinary_errors_do_not_force_reattach(streamer, monkeypatch):
    monkeypatch.setattr(streamer, "ensure_running", lambda: None)
    loop = asyncio.new_event_loop()
    import threading as _t
    t = _t.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        streamer._loop = loop
        streamer._running = True

        async def fails(action):
            return {"ok": False, "error": "element not found"}
        monkeypatch.setattr(streamer, "_do_action", fails)
        streamer._backoff_until = 0.0
        marker = time.monotonic() + 60
        streamer._backoff_until = marker
        out = streamer.run_action({"kind": "click", "value": "nope"})
        assert out == {"ok": False, "error": "element not found"}
        assert streamer._backoff_until == marker   # untouched
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
