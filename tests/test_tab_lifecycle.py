"""Opening and closing tabs goes through raw CDP, not Playwright's wrappers.

MEASURED 2026-08-07, against the live cockpit: every tab operation took
**8.19 seconds** and returned `ok:false` — while succeeding. The tab really
opened, the tab really closed, and the cockpit reported failure, because 8.19s
is the future timeout in close_tab()/new_tab() and the work finished after it.
the owner: "when the user x's out the last tab, the interface kinda hangs for a long
while before anything happens."

It was never the browser. On that same Chrome, raw `Target.createTarget` took
0.022s and `Target.closeTarget` 0.011s, and a FRESH Playwright connection did
new_page() in 0.078s. Only the operator's long-lived connect_over_cdp handle is
slow at page lifecycle — the same desync this file already routes clicks, keys,
navigation and screenshots around. `new_page()` and `page.close()` were the two
that never got moved.

So: same doctrine as the rest of the file. Raw CDP with a bound, Playwright as
the fallback, and an outer timeout LONGER than the work it is timing rather
than shorter.
"""
from __future__ import annotations

import asyncio

import pytest

import operator_prefs as PREFS
import operator_view as OV


class Sess:
    """Browser-level CDP session.

    Models the part that matters: createTarget makes a page APPEAR in the
    context (Chrome creates it; Playwright picks it up off its event stream a
    beat later), and closeTarget makes one go closed. Without that the code
    under test correctly concludes the target never showed up.
    """

    def __init__(self, ctx=None, fail_on: set[str] | None = None):
        self.sent: list[tuple] = []
        self.fail_on = fail_on or set()
        self.ctx = ctx

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method in self.fail_on:
            raise RuntimeError(f"{method} refused")
        if method == "Target.createTarget":
            if self.ctx is not None:
                self.ctx.pages.append(Page(self.ctx, (params or {}).get("url", "")))
            return {"targetId": "T-new"}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "T-page"}}
        if method == "Target.closeTarget":
            if self.ctx is not None:
                for pg in self.ctx.pages:
                    if getattr(pg, "_doomed", False):
                        pg._closed = True
            return {}
        return {}

    def methods(self):
        return [m for m, _ in self.sent]


class Page:
    def __init__(self, ctx, url="https://example.com"):
        self._ctx, self.url = ctx, url
        self._closed = False
        self.pw_closed = 0

    def is_closed(self):
        return self._closed

    @property
    def context(self):
        return self._ctx

    async def close(self):            # the SLOW path — must not be the default
        self.pw_closed += 1
        self._closed = True

    async def bring_to_front(self):
        pass

    async def title(self):
        return "t"


class Ctx:
    def __init__(self, n=1):
        self.pages = [Page(self) for _ in range(n)]
        self.pw_new_pages = 0

    async def new_page(self):         # the SLOW path — must not be the default
        self.pw_new_pages += 1
        p = Page(self, "about:blank")
        self.pages.append(p)
        return p

    async def new_cdp_session(self, page):
        return Sess()


class Browser:
    def __init__(self, ctx, sess):
        self.contexts = [ctx]
        self._sess = sess
        self.browser_sessions = 0

    async def new_browser_cdp_session(self):
        self.browser_sessions += 1
        return self._sess


@pytest.fixture()
def st(monkeypatch, tmp_path):
    monkeypatch.setattr(PREFS, "_PATH", str(tmp_path / "prefs.json"))
    PREFS._cache_clear()
    s = OV._Streamer()
    # the cosmetic per-target work is exercised elsewhere; it is not what these
    # tests are about and it needs a real CDP surface
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(OV._Streamer, "_force_desktop_page", _noop)
    monkeypatch.setattr(OV._Streamer, "_grab", _noop)
    monkeypatch.setattr(OV._Streamer, "_cdp_navigate", _noop)
    monkeypatch.setattr(OV._Streamer, "_update_viewport", lambda self: None)
    return s


def _wire(st, ctx, sess):
    st._browser = Browser(ctx, sess)
    st._page = ctx.pages[0]
    return st._browser


# ── opening ────────────────────────────────────────────────────────────────

def test_new_tab_uses_raw_cdp_not_playwright(st):
    ctx = Ctx(1); sess = Sess(ctx)
    _wire(st, ctx, sess)
    out = asyncio.run(st._new_tab_locked())
    assert out["ok"] is True
    assert "Target.createTarget" in sess.methods()
    assert ctx.pw_new_pages == 0, "fell back to the 8-second Playwright call"


def test_new_tab_opens_the_configured_homepage(st):
    PREFS.set_homepage("https://news.ycombinator.com")
    ctx = Ctx(1); sess = Sess(ctx)
    _wire(st, ctx, sess)
    asyncio.run(st._new_tab_locked())
    created = [p for m, p in sess.sent if m == "Target.createTarget"]
    assert created and created[0]["url"] == "https://news.ycombinator.com"


def test_new_tab_falls_back_to_playwright_when_cdp_refuses(st):
    """Raw CDP is the fast path, not the only path. A Chrome that refuses
    createTarget must still get a tab."""
    ctx = Ctx(1); sess = Sess(ctx, fail_on={"Target.createTarget"})
    _wire(st, ctx, sess)
    out = asyncio.run(st._new_tab_locked())
    assert out["ok"] is True
    assert ctx.pw_new_pages == 1


# ── closing ────────────────────────────────────────────────────────────────

def test_close_tab_uses_raw_cdp_not_playwright(st):
    ctx = Ctx(2); sess = Sess(ctx)
    _wire(st, ctx, sess)
    doomed = ctx.pages[1]
    doomed._doomed = True
    out = asyncio.run(st._close_tab_locked(1))
    assert out["ok"] is True
    assert "Target.closeTarget" in sess.methods()
    assert doomed.pw_closed == 0, "fell back to the 8-second Playwright call"


def test_closing_the_last_tab_resets_it_to_the_homepage(st):
    """The last tab is never really closed — closing it would take the browser
    with it. It is navigated home instead, which is the case the owner was watching
    hang."""
    PREFS.set_homepage("https://example.org")
    ctx = Ctx(1); sess = Sess(ctx)
    _wire(st, ctx, sess)
    navigated = []

    async def _nav(self, page, url, timeout=4):
        navigated.append(url)

    OV._Streamer._cdp_navigate = _nav
    out = asyncio.run(st._close_tab_locked(0))
    assert out == {"ok": True, "reset": True}
    assert navigated == ["https://example.org"]
    assert ctx.pages[0].pw_closed == 0, "the last tab must never be closed"


def test_close_tab_falls_back_when_cdp_refuses(st):
    ctx = Ctx(2); sess = Sess(ctx, fail_on={"Target.closeTarget"})
    _wire(st, ctx, sess)
    doomed = ctx.pages[1]
    doomed._doomed = True
    out = asyncio.run(st._close_tab_locked(1))
    assert out["ok"] is True
    assert doomed.pw_closed == 1


def test_bad_index_is_still_rejected(st):
    ctx = Ctx(2); sess = Sess(ctx)
    _wire(st, ctx, sess)
    assert asyncio.run(st._close_tab_locked(9))["ok"] is False


# ── the timeout that reported success as failure ───────────────────────────

def test_outer_timeout_exceeds_the_inner_budget():
    """8s was SHORTER than what the body could legitimately spend, so a slow
    but successful op always came back ok:false with an empty error string.
    Whatever the bound is, it has to be larger than the work beneath it."""
    assert OV.TAB_OP_TIMEOUT >= OV.TAB_INNER_BUDGET, (
        "the future timeout is shorter than the work it times — successful "
        "tab ops will report failure again")


def test_timeout_reports_a_readable_error(monkeypatch, st):
    """concurrent.futures.TimeoutError stringifies to '' — the cockpit showed
    a failure with no reason at all."""
    st._loop = object()

    class _Fut:
        def result(self, timeout=None):
            import concurrent.futures
            raise concurrent.futures.TimeoutError()

    monkeypatch.setattr(OV.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: (coro.close(), _Fut())[1])
    out = st.new_tab()
    assert out["ok"] is False
    assert out["error"], "empty error string tells the user nothing"
    assert "timed out" in out["error"].lower()
