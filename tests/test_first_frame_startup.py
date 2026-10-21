"""First pixels must not wait for optional attachment housekeeping."""
import asyncio
import time

from test_streamer_lifecycle import FakeCtx, FakePW, _install, streamer  # noqa: F401
import operator_view as OV


def test_first_capture_precedes_optional_hooks_and_other_tab_setup(monkeypatch, streamer):
    ctx = FakeCtx(n_pages=2)
    for page in ctx.pages:
        page.url = 'https://example.com/'
    _install(monkeypatch, [FakePW(ctx=ctx)])
    monkeypatch.setenv('OPERATOR_FILE_BRIDGE', '0')
    events = []

    async def choose(pages):
        return pages[0]

    async def configure(page):
        events.append(('configure', page))

    async def capture(page):
        events.append(('capture', page))
        return OV._PLACEHOLDER_JPEG

    async def hook(script):
        assert streamer.frame, 'optional hooks must follow published pixels'
        events.append(('hook', None))

    monkeypatch.setattr(streamer, '_choose_attach_page', choose)
    monkeypatch.setattr(streamer, '_force_desktop_page', configure)
    monkeypatch.setattr(streamer, '_grab', capture)
    monkeypatch.setattr(ctx, 'add_init_script', hook)
    asyncio.run(streamer._attach())
    assert events[:2] == [('configure', ctx.pages[0]), ('capture', ctx.pages[0])]
    assert streamer.frame == OV._PLACEHOLDER_JPEG
    assert ('hook', None) in events
    stages = streamer._startup_ms
    assert 0 <= stages['browser_ready'] <= stages['cdp_attached'] <= stages['target_ready'] <= stages['first_frame']


def test_idle_attachment_parks_without_capturing_then_resumes(monkeypatch, streamer):
    streamer._running = True
    streamer.last_view = time.monotonic() - OV.IDLE_STOP_AFTER - 1
    captures = []
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        streamer.last_view = time.monotonic()

    async def refresh():
        captures.append('resumed')
        streamer._running = False
        raise RuntimeError('end fixture')

    monkeypatch.setattr(OV.asyncio, 'sleep', sleep)
    monkeypatch.setattr(streamer, '_refresh_active_page', refresh)
    asyncio.run(streamer._grab_loop_inner())
    assert sleeps
    assert captures == ['resumed']


def test_warm_attachment_still_expires(monkeypatch, streamer):
    streamer._running = True
    streamer.last_view = time.monotonic() - 3 * OV.IDLE_STOP_AFTER - 1

    async def unexpected_capture():
        raise AssertionError('expired attachment must leave the capture loop')

    monkeypatch.setattr(streamer, '_refresh_active_page', unexpected_capture)
    asyncio.run(streamer._grab_loop_inner())


def test_slow_optional_hooks_cannot_cancel_a_published_attach(monkeypatch, streamer):
    """2026-10-15: the first frame landed at 6.8 s, the optional hooks ran the
    attach past its 10 s budget, and the timeout tore down a working feed."""
    ctx = FakeCtx(n_pages=1)
    ctx.pages[0].url = 'https://example.com/'
    _install(monkeypatch, [FakePW(ctx=ctx)])
    monkeypatch.setenv('OPERATOR_FILE_BRIDGE', '0')
    monkeypatch.setattr(OV, 'BROWSER_ATTACH_TIMEOUT', 0.2)
    monkeypatch.setattr(OV, 'ATTACH_HOOK_TIMEOUT', 0.05)

    async def choose(pages):
        return pages[0]

    async def configure(page):
        return None

    async def capture(page):
        return OV._PLACEHOLDER_JPEG

    async def stalled_hook(script):
        await asyncio.sleep(5)

    monkeypatch.setattr(streamer, '_choose_attach_page', choose)
    monkeypatch.setattr(streamer, '_force_desktop_page', configure)
    monkeypatch.setattr(streamer, '_grab', capture)
    monkeypatch.setattr(ctx, 'add_init_script', stalled_hook)
    started = time.monotonic()
    asyncio.run(streamer._attach())
    assert time.monotonic() - started < 1.0
    assert streamer.status == 'live'
    assert streamer.frame == OV._PLACEHOLDER_JPEG


def test_attach_budget_still_bounds_the_path_to_first_pixels(monkeypatch, streamer):
    ctx = FakeCtx(n_pages=1)
    ctx.pages[0].url = 'https://example.com/'
    _install(monkeypatch, [FakePW(ctx=ctx)])
    monkeypatch.setenv('OPERATOR_FILE_BRIDGE', '0')
    monkeypatch.setattr(OV, 'BROWSER_ATTACH_TIMEOUT', 0.1)

    async def choose(pages):
        return pages[0]

    async def stalled_configure(page):
        await asyncio.sleep(5)

    monkeypatch.setattr(streamer, '_choose_attach_page', choose)
    monkeypatch.setattr(streamer, '_force_desktop_page', stalled_configure)
    try:
        asyncio.run(streamer._attach())
    except TimeoutError as e:
        assert 'timed out' in str(e)
    else:
        raise AssertionError('a stalled first-pixel path must still time out')
