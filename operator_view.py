"""Browser operator — live view + full remote control of the logged-in Chrome.

One self-contained surface (full-screen on an iPad over Tailscale) that shows the
real Chrome the host's computer-use drives and lets you take the wheel live —
click, type, navigate — interleaving freely with whatever a bot is doing in the
same browser (shared mouse; last action wins). "See it, steer it." (the owner
2026-06-25; refined for click/keyboard control + more controls 2026-06-26.)

Zero new deps — playwright + aiohttp are already in the host-app venv:
  - VIEW: a background thread holds a Playwright connect_over_cdp() attach to the
    Chrome on :9222 and grabs JPEG frames of the active page into a buffer. The
    Flask route streams that as multipart/x-mixed-replace (MJPEG) → an <img>.
  - CONTROL: POST actions run on the SAME attached page. Coordinate clicks come in
    normalized (0..1) so the frontend needn't know the viewport; we scale to the
    live viewport size (also reported to the frontend for letterbox mapping).
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from flask import Blueprint, Response, jsonify, render_template, request
import operator_agent  # the headless-claude agent runner (option 1)
import operator_tasks as operator_tasks_store  # saved-task store (#30)

import os as _os_cfg
# DEMO isolation (the public demo): a second instance runs with OPERATOR_DEMO=1 and
# its own isolated, NOT-logged-in Chrome on a separate CDP port. These env vars are
# unset for the owner's live cockpit (-> no behavior change); set only by demo_server.py.
DEMO = _os_cfg.environ.get("OPERATOR_DEMO") == "1"
# both the live _Streamer and the agent MCP attach here in demo mode (isolated
# Chrome), never :9222 (the logged-in browser). The unguessable path gate is the
# WSGI url-prefix mounted by demo_server.py (APPLICATION_ROOT=/<slug>/<hash>).
CDP_URL = _os_cfg.environ.get("OPERATOR_DEMO_CDP") or "http://127.0.0.1:9222"
FRAME_INTERVAL = 0.066     # ~15fps (the owner's pick)
JPEG_QUALITY = 60
IDLE_STOP_AFTER = 90.0

bp = Blueprint("operator", __name__,
                template_folder="templates", static_folder="static",
                static_url_path="/operator-static")

import base64 as _b64ph

# chrome://new-tab-page renders BLANK under --headless=new + --disable-gpu (the demo's
# launch flags) — Chromium's WebUI new-tab surface needs a GPU compositing path that
# isn't there, so the "reset" silently produces an empty page instead of an error.
# A local data: URL bypasses Chrome's internal NTP entirely — no navigation/rendering-
# path quirks — so it always paints. Loaded via RAW CDP Page.navigate, not Playwright's
# page.set_content()/page.goto() — those wait on the page's own lifecycle-event
# machinery, which (like page.mouse/page.evaluate elsewhere in this file) can hang
# indefinitely on a desynced connect_over_cdp page handle. Page.navigate fire-and-forget
# bounded by asyncio.wait_for never blocks the grab loop.
# Default landing page for tab open/close/new-tab/home. the owner wanted the branded
# custom NTP (templates/newtab.html) gone in favor of google.com; chrome://new-tab-page
# renders blank under headless+no-GPU (see comment above), so google.com is the
# option that actually paints. Still navigated via raw CDP Page.navigate.
_NEWTAB_DATA_URL = "https://www.google.com"


# tiny dark placeholder frame (matches --bg) so the MJPEG stream always has
# valid data and the <img> never shows the broken-image glyph before/between
# real captures.
_PLACEHOLDER_JPEG = _b64ph.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCAGQAoADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDx2iiiqEFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAf/2Q=="
)


@dataclass
class _Streamer:
    frame: bytes | None = None
    frame_ts: float = 0.0
    last_view: float = 0.0
    status: str = "idle"          # idle | connecting | live | error
    detail: str = ""
    vw: int = 0                   # live viewport size (for click coord scaling)
    vh: int = 0
    cur_url: str = ""             # URL cached by the async grab loop; read by sync status route
    last_click: tuple = (0.0, 0.0, 0.0)   # (norm_x, norm_y, monotonic_ts) — agent cursor
    zoom: float = 1.0                      # CSS zoom factor for the page (chrome control)
    _thread: threading.Thread | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _running: bool = False
    _page = None
    _pw = None
    _browser = None
    _cdp = None
    _io_lock = None      # asyncio.Lock — serialize grab vs actions on the CDP page
    _user_closed = False  # True when Chrome was closed manually → don't auto-relaunch (the owner)
    _key_repeat = None   # dict[key -> asyncio.Task] — held-key auto-repeat loops

    # ---- lifecycle -------------------------------------------------------
    def ensure_running(self) -> None:
        with self._lock:
            self.last_view = time.monotonic()
            # restart if flagged running but the thread actually died (stale flag)
            alive = self._thread is not None and self._thread.is_alive()
            if self._running and alive:
                return
            self._running = False  # reset a stale flag so we cleanly relaunch
            self._running = True
            self.status = "connecting"
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="operator-streamer")
            self._thread.start()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # asyncio.Lock/Task bind to the loop they're created on. A reattach spins a
        # fresh loop here, so DROP any primitives cached against the previous loop —
        # else they raise "bound to a different event loop" on the next action and the
        # status card flashes "Failed" for every click/keystroke. Rebuilt lazily.
        self._io_lock = None
        self._key_repeat = {}   # drop any Tasks bound to the dead old loop
        try:
            self._loop.run_until_complete(self._grab_loop())
        except Exception as e:  # noqa: BLE001
            self.status, self.detail = "error", str(e)
        finally:
            self._running = False
            if self.status == "live":
                self.status = "idle"

    @staticmethod
    def _chrome_attach_script() -> str:
        """Path to the (re)launcher for the active mode — the demo's isolated
        headless Chrome under DEMO, the owner's logged-in GUI Chrome otherwise."""
        import os
        if DEMO:
            return os.path.expanduser("~/operator-demo/op-demo-chrome.sh")
        return os.path.expanduser("~/agents/browse/chrome-attach.sh")

    def _ensure_chrome_alive(self) -> None:
        """Check whether CDP is reachable and update status accordingly. Does NOT
        launch or kill Chrome — auto-relaunch (on manual close, on wedge, on
        dispatch) was removed 2026-07-05 after it repeatedly spawned duplicate
        Chrome windows: multiple call sites (dispatch, scheduled dispatch, the grab
        loop's wedge detector) each independently decided Chrome was down/wedged
        and each shelled out to chrome-attach.sh around the same time, and even a
        lock around one call site didn't stop the others from racing it. Chrome is
        launched exactly once now, at server startup (see _launch_chrome_on_boot);
        if the owner closes it, restart it yourself via the bot-chrome script/shortcut.
        Blocking + best-effort; runs in the streamer thread before an attach."""
        import urllib.request, json as _json
        alive = False
        try:
            # /json (target list) needs the browser to actually service a request,
            # not just answer /json/version (a wedged Chrome still answers version).
            raw = urllib.request.urlopen(CDP_URL + "/json", timeout=3).read()
            _json.loads(raw)
            alive = True
        except Exception:  # noqa: BLE001 — dead or wedged
            alive = False
        if alive:
            self._user_closed = False   # it's up → clear any manual-close latch
            return
        self._user_closed = True
        self.status, self.detail = "idle", "browser closed — relaunch it via the bot-chrome script"

    async def _attach(self) -> None:
        from playwright.async_api import async_playwright
        self._ensure_chrome_alive()
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(CDP_URL)
        ctx = self._browser.contexts[0] if self._browser.contexts else \
            await self._browser.new_context()
        pages = [p for p in ctx.pages if not p.is_closed()]
        self._page = pages[0] if pages else await ctx.new_page()
        try:
            await ctx.add_init_script("""
                (function(){
                  if (window.__opClickHooked) return; window.__opClickHooked = true;
                  function rec(e){ try {
                    var w = window.innerWidth || 1, h = window.innerHeight || 1;
                    window.__opClick = { x: e.clientX / w, y: e.clientY / h, t: Date.now() };
                  } catch(_){} }
                  window.addEventListener('pointerdown', rec, true);
                  window.addEventListener('click', rec, true);
                })();
            """)
        except Exception:
            pass
        # re-apply the chosen zoom on every navigation
        try:
            if self.zoom and self.zoom != 1.0:
                await ctx.add_init_script(
                    f"document.addEventListener('DOMContentLoaded',()=>{{document.documentElement.style.zoom='{self.zoom}';}});")
        except Exception:
            pass
        # also install on the CURRENTLY-open page (init script only covers future loads).
        # WRAP IN A TIMEOUT: evaluate() on a privileged page (chrome://new-tab-page) or a
        # busy/heavy page (e.g. Bloomberg mid-load) can BLOCK FOREVER with no built-in
        # timeout, wedging _attach before it ever sets status="live" — the streamer then
        # sits in "connecting" indefinitely and the browser pane never paints. Bounding it
        # means a hostile current page degrades gracefully (no click-hook on it) instead of
        # taking the whole streamer down.
        try:
            await asyncio.wait_for(self._page.evaluate("""
                (function(){
                  if (window.__opClickHooked) return; window.__opClickHooked = true;
                  function rec(e){ try {
                    var w = window.innerWidth || 1, h = window.innerHeight || 1;
                    window.__opClick = { x: e.clientX / w, y: e.clientY / h, t: Date.now() };
                  } catch(_){} }
                  window.addEventListener('pointerdown', rec, true);
                  window.addEventListener('click', rec, true);
                })();
            """), timeout=2.5)
        except Exception:
            pass
        self._update_viewport()
        self.status, self.detail = "live", ""

    def _update_viewport(self) -> None:
        try:
            vp = self._page.viewport_size
            if vp:
                self.vw, self.vh = vp["width"], vp["height"]
        except Exception:  # noqa: BLE001
            pass

    def _iolock(self):
        if self._io_lock is None:
            self._io_lock = asyncio.Lock()
        return self._io_lock

    async def _grab_loop(self) -> None:
        await self._attach()
        _misses = 0
        while self._running:
            if time.monotonic() - self.last_view > IDLE_STOP_AFTER:
                break
            try:
                self._refresh_active_page()
                await self._follow_active_tab()
                async with self._iolock():
                    png = await self._grab(self._page)
                if png:
                    self.frame = png
                    self.frame_ts = time.monotonic()
                    try: self.cur_url = self._page.url or ""
                    except Exception: pass
                    _misses = 0
                else:
                    _misses += 1
                    if _misses >= 4:
                        # wedged Chrome (alive but screenshots hang/fail). No more
                        # auto-relaunch here (2026-07-05 — see _ensure_chrome_alive):
                        # stop cleanly and surface it instead of shelling out to
                        # chrome-attach.sh, which could race a concurrent dispatch's
                        # own relaunch attempt. Manual relaunch via the bot-chrome
                        # script is the expected recovery now; this is rare enough
                        #  that it doesn't need to self-heal.
                        _misses = 0
                        self.status, self.detail = "error", "Chrome wedged — relaunch it via the bot-chrome script"
                        break
                if not self.vw:
                    # CDP-attached pages have NO Playwright viewport_size (it's None for
                    # connect_over_cdp), so the sync helper leaves vw/vh=0 → manual click
                    # mapping breaks. Read the REAL viewport via JS innerWidth/innerHeight.
                    # BOUND IT: page.evaluate() has no built-in timeout and can block
                    # FOREVER on a page whose JS world isn't responsive (observed: a
                    # connect_over_cdp page reporting url=='' yet still screenshottable).
                    # Unbounded, this froze the grab loop after the first frame — the
                    # stream delivered one buffered burst then went silent (status "live",
                    # vw stuck at 0). Bound + CDP-layout fallback so the loop never stalls.
                    try:
                        _vp = await asyncio.wait_for(self._page.evaluate(
                            "({w: window.innerWidth, h: window.innerHeight})"), timeout=1.0)
                        if _vp and _vp.get("w"):
                            self.vw, self.vh = int(_vp["w"]), int(_vp["h"])
                        else:
                            self._update_viewport()
                    except Exception:
                        # JS world slow/unavailable → CDP layout metrics, then sync helper.
                        try:
                            sess = self._cdp or await self._page.context.new_cdp_session(self._page)
                            self._cdp = sess
                            m = await asyncio.wait_for(sess.send("Page.getLayoutMetrics"), timeout=1.0)
                            vv = (m or {}).get("visualViewport") or {}
                            cw, ch = int(vv.get("clientWidth") or 0), int(vv.get("clientHeight") or 0)
                            if cw and ch:
                                self.vw, self.vh = cw, ch
                            else:
                                self._update_viewport()
                        except Exception:
                            self._update_viewport()
            except Exception as e:  # noqa: BLE001
                self.detail = str(e)
                # A single transient capture error is normal during navigation —
                # don't thrash the page/session for it. Only escalate to a reattach
                # after several consecutive failures.
                _misses += 1
                if _misses < 4:
                    await asyncio.sleep(FRAME_INTERVAL)
                    continue
                _misses = 0
                # page-level hiccup → soft swap; whole-browser drop → hard re-attach
                ok = await self._reattach_soft()
                if not ok:
                    self.status = "connecting"
                    try:
                        await self._teardown()
                        await self._attach()
                    except Exception as e2:  # noqa: BLE001
                        self.status, self.detail = "error", str(e2)
                        await asyncio.sleep(1.0)
            # ease off while an agent drives (shares CDP with the agent's MCP)
            try:
                busy = operator_agent.runner.is_running()
            except Exception:
                busy = False
            await asyncio.sleep(0.45 if busy else FRAME_INTERVAL)
        self.frame = None          # stopping → no stale 'live' with no frames
        await self._teardown()

    async def _grab(self, page):
        """Raw JPEG frame via CDP Page.captureScreenshot — no font-loading wait
        (page.screenshot() font-waits and hung 30s on heavy pages). Falls back to
        a short-timeout page.screenshot if CDP isn't available."""
        import base64 as _b64
        try:
            sess = getattr(self, "_cdp", None)
            if sess is None:
                sess = await page.context.new_cdp_session(page)
                self._cdp = sess
            res = await asyncio.wait_for(
                sess.send("Page.captureScreenshot", {"format": "jpeg", "quality": JPEG_QUALITY}),
                timeout=2.5)
            try:
                cr = await asyncio.wait_for(sess.send("Runtime.evaluate", {
                    "expression": "JSON.stringify(window.__opClick||null)",
                    "returnByValue": True}), timeout=0.6)
                val = (cr.get("result") or {}).get("value")
                if val and val != "null":
                    import json as _json
                    d = _json.loads(val)
                    if isinstance(d, dict) and "x" in d:
                        self.last_click = (float(d["x"]), float(d["y"]), time.monotonic())
            except Exception:
                pass
            return _b64.b64decode(res["data"])
        except Exception:
            self._cdp = None  # session may be stale (page nav) — rebuild next time
            try:
                return await asyncio.wait_for(
                    page.screenshot(type="jpeg", quality=JPEG_QUALITY, animations="disabled"),
                    timeout=2.5)
            except Exception:
                return None

    def _refresh_active_page(self) -> None:
        try:
            ctx = self._browser.contexts[0]
            live = [p for p in ctx.pages if not p.is_closed()]
            if not live:
                return
            switch_to = None
            # 1. current page gone → must switch
            if self._page is None or self._page.is_closed():
                switch_to = live[-1]
            else:
                # 2. follow the agent's ACTIVE tab: when a new tab appeared (the live
                # count grew) the agent almost certainly just opened+moved to it, so
                # stream that one. bounded by a count check so we don't churn per-frame.
                n = len(live)
                if n != getattr(self, "_live_n", n) and self._page is not live[-1]:
                    switch_to = live[-1]
                self._live_n = n
            if switch_to is not None and switch_to is not self._page:
                self._page = switch_to
                self._cdp = None
                self._update_viewport()
        except Exception:  # noqa: BLE001
            pass

    async def _follow_active_tab(self) -> None:
        """Stream whichever tab the AGENT (or user) actually has in the FOREGROUND —
        not just the newest one. _refresh_active_page only switches when the tab COUNT
        changes (and always to the last tab), so an agent that flips between already-
        open tabs (clicks a link that activates an existing tab, or switches back to
        tab 1) left the view frozen on the stale tab. Here we poll
        each open page's document.visibilityState — only the foreground tab reports
        'visible' — and follow it. Throttled (every ~0.8s) + bounded per check so it
        never stalls the grab loop, and only does work when there's >1 tab."""
        try:
            now = time.monotonic()
            if now - getattr(self, "_tab_check_ts", 0.0) < 0.8:
                return
            self._tab_check_ts = now
            ctx = self._browser.contexts[0]
            live = [p for p in ctx.pages if not p.is_closed()]
            if len(live) < 2:
                return  # single tab → nothing to follow
            # current page already visible? then don't churn.
            async def _vis(pg):
                try:
                    s = await asyncio.wait_for(
                        pg.evaluate("document.visibilityState"), timeout=0.5)
                    return s
                except Exception:
                    # JS world unavailable → fall back to CDP visibility metric
                    try:
                        sess = await pg.context.new_cdp_session(pg)
                        r = await asyncio.wait_for(
                            sess.send("Runtime.evaluate", {
                                "expression": "document.visibilityState",
                                "returnByValue": True}), timeout=0.5)
                        return (r.get("result") or {}).get("value")
                    except Exception:
                        return None
            # if the page we're on is still visible, keep it (avoid flapping)
            if self._page in live:
                cur_vis = await _vis(self._page)
                if cur_vis == "visible":
                    return
            # find a foreground tab and switch to it
            for pg in reversed(live):   # prefer the newest visible one
                if pg is self._page:
                    continue
                if await _vis(pg) == "visible":
                    self._page = pg
                    self._cdp = None
                    self._update_viewport()
                    return
        except Exception:  # noqa: BLE001
            pass

    async def _reattach_soft(self) -> bool:
        """Swap to a live page in the SAME browser. Returns False if the browser
        connection itself is gone (caller then does a hard re-attach)."""
        try:
            ctx = self._browser.contexts[0]
            live = [p for p in ctx.pages if not p.is_closed()]
            if live:
                self._page = live[-1]
                return True
            return False
        except Exception:  # noqa: BLE001 — browser/context dropped
            return False

    async def _teardown(self) -> None:
        for closer in (lambda: self._browser and self._browser.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                r = closer()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:  # noqa: BLE001
                pass
        self._page = self._browser = self._pw = None
        self.status = "idle"

    # ---- tabs ------------------------------------------------------------
    def list_tabs(self) -> list:
        """Snapshot of open tabs (title/url/active). Runs on the loop thread."""
        if not self._running or self._loop is None:
            self.ensure_running()
            return []
        try:
            fut = asyncio.run_coroutine_threadsafe(self._list_tabs(), self._loop)
            return fut.result(timeout=6)
        except Exception:
            return []

    async def _list_tabs(self) -> list:
        try:
            ctx = self._browser.contexts[0]
            tabs = []
            for i, pg in enumerate(ctx.pages):
                if pg.is_closed():
                    continue
                try:
                    title = await asyncio.wait_for(pg.title(), timeout=2)
                except Exception:
                    title = ""
                tabs.append({"i": i, "title": (title or pg.url or "tab")[:48],
                             "url": pg.url, "active": pg is self._page})
            return tabs
        except Exception:
            return []

    def switch_tab(self, idx: int) -> dict:
        if self._loop is None:
            return {"ok": False, "error": "not running"}
        try:
            fut = asyncio.run_coroutine_threadsafe(self._switch_tab(idx), self._loop)
            return fut.result(timeout=8)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    async def _switch_tab(self, idx: int) -> dict:
        try:
            ctx = self._browser.contexts[0]
            pages = [p for p in ctx.pages if not p.is_closed()]
            if 0 <= idx < len(pages):
                self._page = pages[idx]
                self._cdp = None
                await self._page.bring_to_front()
                self._update_viewport()
                return {"ok": True}
            return {"ok": False, "error": "bad tab index"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def close_tab(self, idx: int) -> dict:
        if self._loop is None: return {"ok": False, "error": "not running"}
        try:
            return asyncio.run_coroutine_threadsafe(self._close_tab(idx), self._loop).result(timeout=8)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    async def _close_tab(self, idx: int) -> dict:
        try:
            ctx = self._browser.contexts[0]
            pages = [p for p in ctx.pages if not p.is_closed()]
            if 0 <= idx < len(pages):
                closing = pages[idx]
                # never close the LAST tab — that kills the browser / leaves the
                # viewer with nothing + no way to reopen. Navigate it to Google.
                if len(pages) <= 1:
                    # last tab: don't close it (that kills the browser) — reset it to
                    # our own New Tab page instead of chrome://new-tab-page (which
                    # renders blank under headless+no-GPU — see _NEWTAB_HTML comment).
                    self._cdp = None
                    try:
                        await self._cdp_navigate(closing, _NEWTAB_DATA_URL)
                    except Exception:  # noqa: BLE001
                        pass
                    self._page = closing; self._cdp = None; self._update_viewport()
                    return {"ok": True, "reset": True}
                await closing.close()
                live = [p for p in ctx.pages if not p.is_closed()]
                if not live:
                    # safety net: never leave zero tabs (that closes the browser) —
                    # open a fresh one so the demo/cockpit always has a live page.
                    try:
                        newp = await ctx.new_page()
                        self._cdp = None
                        await self._cdp_navigate(newp, _NEWTAB_DATA_URL)
                        live = [newp]
                    except Exception:  # noqa: BLE001
                        live = []
                if live:
                    self._page = live[-1]; self._cdp = None; self._update_viewport()
                return {"ok": True}
            return {"ok": False, "error": "bad tab index"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def new_tab(self) -> dict:
        if self._loop is None: return {"ok": False, "error": "not running"}
        try:
            return asyncio.run_coroutine_threadsafe(self._new_tab(), self._loop).result(timeout=8)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    async def _new_tab(self) -> dict:
        try:
            ctx = self._browser.contexts[0]
            pg = await ctx.new_page()
            self._cdp = None
            await self._cdp_navigate(pg, _NEWTAB_DATA_URL)
            self._page = pg; self._cdp = None; self._update_viewport()
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ---- actions ---------------------------------------------------------
    def run_action(self, action: dict) -> dict:
        if not self._running or self._loop is None:
            self.ensure_running()
            time.sleep(0.5)
        if self._loop is None:
            return {"ok": False, "error": "streamer not running"}
        fut = asyncio.run_coroutine_threadsafe(self._do_action(action), self._loop)
        try:
            return fut.result(timeout=30)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def _safe_url(self, p) -> str:
        """p.url is a sync property but on a desynced connect_over_cdp page it can
        return '' (handle out of sync). Never raises; returns '' on trouble."""
        try:
            return p.url or ""
        except Exception:
            return ""

    async def _cdp_session(self, p):
        """Reusable CDP session for raw input/screenshot ops. Rebuilt if missing."""
        sess = getattr(self, "_cdp", None)
        if sess is None:
            sess = await p.context.new_cdp_session(p)
            self._cdp = sess
        return sess

    async def _cdp_click(self, p, x: float, y: float, button: str = "left",
                         clicks: int = 1, ramp: bool = True) -> None:
        """Click at CSS-px (x,y) via raw CDP Input.dispatchMouseEvent, bypassing
        Playwright's high-level page.mouse (which blocks indefinitely on a desynced
        connect_over_cdp handle). Each op is timeout-bounded so a wedged page can
        never hold _io_lock and freeze the grab loop. Also stamps last_click so the
        UI cursor overlay shows even if the page's own __opClick JS hook is slow."""
        sess = await self._cdp_session(p)
        async def _send(typ, **extra):
            args = {"type": typ, "x": float(x), "y": float(y)}
            args.update(extra)
            await asyncio.wait_for(sess.send("Input.dispatchMouseEvent", args), timeout=4)
        # glide a couple of moves in so it isn't a zero-movement instant click
        await _send("mouseMoved")
        await asyncio.sleep(0.02)
        if ramp:
            # programmatic multi-click (agent dblclick): synthesize the FULL
            # sequence — press/release 1, press/release 2, … up to `clicks`.
            for n in range(1, clicks + 1):
                await _send("mousePressed", button=button, clickCount=n)
                await asyncio.sleep(0.03)
                await _send("mouseReleased", button=button, clickCount=n)
        else:
            # incremental user multi-click: each physical click of a burst arrives
            # as its own steer with the native detail count. The earlier clicks in
            # the burst were already dispatched (clickCount 1, 2, …), so send ONLY
            # the nth press/release — Chrome's input pipeline turns clickCount=2/3
            # into the page's dblclick / word-select / paragraph-select behavior.
            await _send("mousePressed", button=button, clickCount=clicks)
            await asyncio.sleep(0.03)
            await _send("mouseReleased", button=button, clickCount=clicks)
        # stamp the cursor overlay from the normalized coords we were handed
        try:
            d = await self._viewport_dims(p)
            if d.get("w") and d.get("h"):
                self.last_click = (x / d["w"], y / d["h"], time.monotonic())
        except Exception:
            pass

    async def _cdp_navigate(self, p, url: str, timeout: float = 4) -> None:
        """Navigate via raw CDP Page.navigate, bypassing Playwright's
        page.goto()/set_content() (which wait on lifecycle events that can hang
        indefinitely on a desynced connect_over_cdp handle — same bug class as
        page.mouse/page.evaluate, see _cdp_click). Caller must null self._cdp
        first if p differs from the page the cached session is bound to."""
        sess = await self._cdp_session(p)
        await asyncio.wait_for(sess.send("Page.navigate", {"url": url}), timeout=timeout)

    async def _viewport_dims(self, p):
        """CSS-pixel viewport {w,h} for mapping normalized click coords. Uses CDP
        getLayoutMetrics (immune to page eval-blocking, e.g. Amex CSP). Falls back
        to page.evaluate, then to the cached streamer dims."""
        # 1) CDP — works even when the page disables eval()
        try:
            sess = getattr(self, "_cdp", None)
            if sess is None:
                sess = await p.context.new_cdp_session(p)
                self._cdp = sess
            m = await asyncio.wait_for(sess.send("Page.getLayoutMetrics"), timeout=3)
            vp = m.get("cssLayoutViewport") or m.get("layoutViewport") or {}
            w = vp.get("clientWidth"); h = vp.get("clientHeight")
            if w and h:
                return {"w": w, "h": h}
        except Exception:
            self._cdp = None
        # 2) page eval (works on normal sites)
        try:
            d = await p.evaluate("({w: window.innerWidth, h: window.innerHeight})")
            if d.get("w") and d.get("h"):
                return d
        except Exception:
            pass
        # 3) last resort: the dims the screenshot frame was captured at
        if self.vw and self.vh:
            return {"w": self.vw, "h": self.vh}
        return {"w": 1280, "h": 800}

    async def _do_action(self, action: dict) -> dict:
        if self._page is None:
            return {"ok": False, "error": "no page attached"}
        kind = action.get("kind")
        val = action.get("value", "")
        p = self._page
        _lk = self._iolock()
        await _lk.acquire()
        try:
            if kind == "goto":
                url = val if "://" in val else f"https://{val}"
                await p.goto(url, wait_until="domcontentloaded", timeout=20000)
            elif kind == "click":                       # click by visible text
                await p.get_by_text(val, exact=False).first.click(timeout=8000)
            elif kind in ("click_at", "dblclick_at"):   # (double-)click at normalized x,y
                # CDP-attached Chrome reports viewport_size=None, so mouse.click
                # must scale against the LIVE CSS-pixel viewport (window.inner*),
                # which is also exactly what the screenshot frame covers.
                dims = await self._viewport_dims(p)
                x = float(action.get("x", 0)) * dims["w"]
                y = float(action.get("y", 0)) * dims["h"]
                # Drive the click via RAW CDP Input.dispatchMouseEvent, not
                # p.mouse.*. With Playwright 1.60 + headless Chrome the connect_over_cdp
                # page wrapper intermittently desyncs (url=='' , JS world dead) — its
                # high-level mouse/evaluate/title calls then BLOCK with no timeout,
                # holding _io_lock and freezing the grab loop (the "click crashes the
                # feed, no cursor" bug). Raw CDP bypasses the broken page model — it's
                # the same layer _grab uses for screenshots, which never broke.
                # `count` = native multi-click detail from the cockpit (1 single,
                # 2 double, 3 triple → sentence/paragraph select). Sent per physical
                # click, so dispatch it incrementally (ramp=False). The agent's
                # dblclick_at carries no count and keeps the full ramped sequence.
                cnt = action.get("count")
                try:
                    cnt = max(1, min(4, int(cnt))) if cnt is not None else None
                except (TypeError, ValueError):
                    cnt = None
                if cnt is not None:
                    await self._cdp_click(p, x, y, button="left",
                                          clicks=cnt, ramp=False)
                else:
                    clicks = 2 if kind == "dblclick_at" else 1
                    await self._cdp_click(p, x, y, button="left", clicks=clicks)
                _u = self._safe_url(p); self.cur_url = _u or self.cur_url
                return {"ok": True, "url": _u, "px": [round(x), round(y)]}
            elif kind == "rclick_at":              # right-click at normalized x,y (context menu)
                dims = await self._viewport_dims(p)
                x = float(action.get("x", 0)) * dims["w"]
                y = float(action.get("y", 0)) * dims["h"]
                await self._cdp_click(p, x, y, button="right", clicks=1)
            elif kind == "type":
                await p.keyboard.type(val, delay=35)
            elif kind == "key":
                await p.keyboard.press(val or "Enter")
            elif kind == "key_down":
                # HELD-KEY AUTO-REPEAT. Playwright's keyboard.down() fires ONE keydown and
                # does NOT auto-repeat like a physically-held key — so a held arrow scrolled
                # once, not continuously. Instead we simulate OS key-repeat: press once now,
                # then a background task re-presses every ~45ms until key_up. Each press is
                # a real down+up so the page scrolls/navigates each tick.
                key = val or "Enter"
                if self._key_repeat is None:
                    self._key_repeat = {}
                old = self._key_repeat.pop(key, None)
                if old:
                    old.cancel()
                await p.keyboard.press(key)                 # immediate first tick
                self._key_repeat[key] = asyncio.ensure_future(self._repeat_key(key))
            elif kind == "key_up":            # stop the held-key repeat
                key = val or "Enter"
                if self._key_repeat:
                    t = self._key_repeat.pop(key, None)
                    if t:
                        t.cancel()
            elif kind == "scroll":
                # numeric dy/dx → precise user wheel/touch scroll; else keyword amounts.
                dx = action.get("dx"); dy = action.get("dy")
                if isinstance(dy, (int, float)) or isinstance(dx, (int, float)):
                    await p.mouse.wheel(float(dx or 0), float(dy or 0))
                else:
                    amt = {"up": -600, "down": 600, "top": -100000,
                           "bottom": 100000}.get(val, 600)
                    await p.mouse.wheel(0, amt)
            elif kind == "back":
                # wait_until="commit" returns as soon as the navigation COMMITS (not
                # full load), so we don't hold the io-lock for up to 15s while the page
                # loads — that lock starves the grab loop and froze/broke the feed on
                # back/forward (the owner). The feed then streams the new page as it loads.
                await p.go_back(wait_until="commit", timeout=8000)
            elif kind == "forward":
                await p.go_forward(wait_until="commit", timeout=8000)
            elif kind == "reload":
                await p.reload(wait_until="commit", timeout=8000)
            elif kind in ("mousedown_at", "mouseup_at"):  # press-and-hold (captchas)
                dims = await self._viewport_dims(p)
                x = float(action.get("x", 0)) * dims["w"]
                y = float(action.get("y", 0)) * dims["h"]
                if kind == "mousedown_at":
                    await p.mouse.move(x, y, steps=6)
                    await p.mouse.down()
                else:
                    await p.mouse.up()
                _u = self._safe_url(p); self.cur_url = _u or self.cur_url
                return {"ok": True, "url": _u, "px": [round(x), round(y)]}
            elif kind == "drag":          # atomic click-drag: down at (x0,y0) → up at (x1,y1)
                dims = await self._viewport_dims(p)
                x0 = float(action.get("x0", 0)) * dims["w"]
                y0 = float(action.get("y0", 0)) * dims["h"]
                x1 = float(action.get("x1", 0)) * dims["w"]
                y1 = float(action.get("y1", 0)) * dims["h"]
                await p.mouse.move(x0, y0, steps=4)
                await p.mouse.down()
                await asyncio.sleep(0.05)
                await p.mouse.move(x1, y1, steps=20)   # glide so the page sees a drag
                await asyncio.sleep(0.05)
                await p.mouse.up()
                _u = self._safe_url(p); self.cur_url = _u or self.cur_url
                return {"ok": True, "url": _u, "px": [round(x1), round(y1)]}
            elif kind == "find":          # ⌘F find-on-page
                await p.keyboard.press("Control+f")
            elif kind == "select_all":
                await p.keyboard.press("Control+a")
            elif kind == "zoom":                        # browser zoom in/out/reset
                # synthetic Ctrl+/- didn't visibly zoom a CDP-driven Chrome, so apply
                # a CSS zoom on the document instead (reliable, captured by the feed,
                # re-applied after each navigation via the init script below).
                if val == "in":
                    self.zoom = min(3.0, round(self.zoom + 0.1, 2))
                elif val == "out":
                    self.zoom = max(0.3, round(self.zoom - 0.1, 2))
                else:
                    self.zoom = 1.0
                try:
                    await p.evaluate(f"document.documentElement.style.zoom = '{self.zoom}'")
                except Exception:
                    pass
            elif kind == "hard_reload":
                await p.reload(timeout=20000)
                await p.keyboard.press("Control+Shift+r")
            elif kind == "home":
                await self._cdp_navigate(p, _NEWTAB_DATA_URL)
            elif kind == "tab_next":                    # cycle to the next tab
                ctx = self._browser.contexts[0]
                live = [pg for pg in ctx.pages if not pg.is_closed()]
                if len(live) > 1 and self._page in live:
                    nxt = live[(live.index(self._page) + 1) % len(live)]
                    await nxt.bring_to_front()
                    self._page = nxt
                    self._update_viewport()
            else:
                return {"ok": False, "error": f"unknown action '{kind}'"}
            _u = self._safe_url(p); self.cur_url = _u or self.cur_url
            return {"ok": True, "url": _u}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        finally:
            try: _lk.release()
            except Exception: pass

    async def _repeat_key(self, key: str) -> None:
        """Simulate OS key auto-repeat for a held key: re-press every ~45ms until
        cancelled (key_up). Each press is a real down+up so the page keeps scrolling/
        navigating. Acquires the io-lock per tick so it doesn't race the grab loop.
        Self-terminates if the page goes away. Cancellation is the normal exit."""
        try:
            await asyncio.sleep(0.28)   # honor the OS repeat-delay before the first repeat
            lk = self._iolock()
            while True:
                p = self._page
                if p is None:
                    break
                await lk.acquire()
                try:
                    await p.keyboard.press(key)
                except Exception:
                    break
                finally:
                    try: lk.release()
                    except Exception: pass
                await asyncio.sleep(0.045)   # ~22 presses/sec → smooth continuous scroll
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self._key_repeat:
                self._key_repeat.pop(key, None)


_streamer = _Streamer()


def _launch_chrome_on_boot() -> None:
    """Launch the bot Chrome exactly once, when the server process starts —
    the only place Chrome gets auto-started now (2026-07-05; see
    _ensure_chrome_alive for why the old on-demand/on-wedge/on-dispatch
    auto-relaunches were removed). chrome-attach.sh is itself idempotent
    (it no-ops if CDP is already answering), so this is safe even if Chrome
    was already left running from a previous session. Runs in a background
    thread so a slow/hung Windows-side launch never blocks server startup."""
    import os, subprocess
    attach = _Streamer._chrome_attach_script()
    if not os.path.exists(attach):
        return
    try:
        subprocess.Popen(["bash", attach], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


threading.Thread(target=_launch_chrome_on_boot, daemon=True, name="operator-chrome-boot").start()


# ── routes ────────────────────────────────────────────────────────────────
@bp.route("/operator")
def operator_page():
    from flask import make_response
    # demo: serve the standalone, de-PII'd template (no squad chrome/nav, no owner
    # refs, bot picker collapsed). Regenerate with gen_demo_template.py.
    _tmpl = "operator_demo.html" if DEMO else "operator.html"
    resp = make_response(render_template(_tmpl))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@bp.route("/demo")
def operator_demo_page():
    """Demo entry path alias (the public demo URL ends in /demo, not /operator —
    version-agnostic). Serves the same page; only meaningful when DEMO."""
    return operator_page()


@bp.route("/cockpit")
def _cockpit_redirect():  # legacy path → operator
    from flask import redirect, url_for
    return redirect(url_for("operator.operator_page"))


@bp.route("/operator/stream")
def operator_stream():
    """MJPEG multipart stream — renders into an <img>. Survives frame gaps."""
    _streamer.ensure_running()

    def _part(jpeg):
        return (b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                + jpeg + b"\r\n")

    def gen():
        # Emit a placeholder frame IMMEDIATELY so the <img> always has valid
        # multipart data and never shows the broken-image glyph, even before the
        # first real capture (cold start / mid-reattach). We then keep the
        # connection open forever, swapping in real frames as they arrive.
        yield _part(_PLACEHOLDER_JPEG)
        last_sent = -1.0
        last_push = 0.0
        # Poll the frame buffer MUCH faster than the capture cadence so a fresh
        # frame is pushed within a few ms of being grabbed (snappy feed) — we only
        # actually yield when the frame is NEW, so the fast poll adds no bandwidth.
        POLL = 0.02
        while True:
            _streamer.last_view = time.monotonic()
            f = _streamer.frame
            ts = _streamer.frame_ts
            now = time.monotonic()
            if f and ts != last_sent:
                last_sent = ts; last_push = now      # push a new frame immediately
                yield _part(f)
            elif f and (now - last_push) > 1.0:
                last_push = now                       # ~1s heartbeat of last frame
                yield _part(f)
            elif not f and (now - last_push) > 1.0:
                last_push = now                       # placeholder heartbeat (no frame yet)
                yield _part(_PLACEHOLDER_JPEG)
            time.sleep(POLL)

    resp = Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/operator/tabs")
def operator_tabs():
    return jsonify(tabs=_streamer.list_tabs())


@bp.route("/operator/tab/<int:idx>", methods=["POST"])
def operator_tab_switch(idx):
    return jsonify(_streamer.switch_tab(idx))


@bp.route("/operator/tab/<int:idx>/close", methods=["POST"])
def operator_tab_close(idx):
    return jsonify(_streamer.close_tab(idx))


@bp.route("/operator/tab/new", methods=["POST"])
def operator_tab_new():
    return jsonify(_streamer.new_tab())


# screenshot dir the computer-use MCP writes into (same knob as playwright-mcp.sh).
# Served read-only so a screenshot the agent references inline (![](file://.../x.png))
# can render in the chat instead of being stripped to a text note.
_SHOT_DIR = _os_cfg.path.realpath(_os_cfg.path.expanduser(
    _os_cfg.environ.get("COMPUTER_USE_OUTPUT_DIR")
    or _os_cfg.environ.get("PLAYWRIGHT_OUTPUT_DIR")
    or "~/.cache/computer-use"))


@bp.route("/operator/shot/<path:name>")
def operator_shot(name):
    """Serve an agent screenshot PNG/JPG by basename from the computer-use output
    dir. Basename-only + extension whitelist + realpath containment → no traversal."""
    from flask import send_from_directory, abort
    base = _os_cfg.path.basename(name)            # strip any path components
    if not base or base != name or base.startswith("."):
        abort(404)
    if _os_cfg.path.splitext(base)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        abort(404)
    target = _os_cfg.path.realpath(_os_cfg.path.join(_SHOT_DIR, base))
    if _os_cfg.path.commonpath([target, _SHOT_DIR]) != _SHOT_DIR or not _os_cfg.path.isfile(target):
        abort(404)
    resp = send_from_directory(_SHOT_DIR, base)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@bp.route("/operator/status")
def operator_status():
    _streamer.last_view = time.monotonic()
    # cockpit is open and polling → any finished-run badge is "seen"
    if not DEMO:
        try:
            import operator_schedule as _os_mod
            _os_mod.clear_unseen()
        except Exception:
            pass
    fresh = (_streamer.frame is not None
             and (time.monotonic() - _streamer.frame_ts) < 6.5)
    cur_url = _streamer.cur_url
    lx, ly, lt = _streamer.last_click
    click = None
    if lt and (time.monotonic() - lt) < 1.2:
        click = {"x": round(lx, 4), "y": round(ly, 4), "age": round(time.monotonic() - lt, 3)}
    return jsonify(status=_streamer.status, detail=_streamer.detail,
                   has_frame=fresh, vw=_streamer.vw, vh=_streamer.vh, url=cur_url,
                   click=click)


@bp.route("/operator/unseen")
def operator_unseen():
    """Finished-runs-you-haven't-looked-at count — feeds the red badge on the
    host-app operator nav tab. Always 0 in the demo (no scheduler there)."""
    if DEMO:
        return jsonify(count=0)
    try:
        import operator_schedule as _os_mod
        return jsonify(count=_os_mod.unseen_count())
    except Exception:
        return jsonify(count=0)


@bp.route("/operator/steer", methods=["POST"])
def operator_steer():
    data = request.get_json(silent=True) or request.form
    action = {"kind": data.get("kind"), "value": data.get("value", ""),
              "x": data.get("x", 0), "y": data.get("y", 0),
              # dx/dy carry the wheel/touch scroll delta (kind=="scroll"). Must default
              # to None, NOT 0 — _do_action tells "a real delta was sent" apart from
              # "no delta, use the up/down/top/bottom keyword" via isinstance(dy, (int,
              # float)), and 0 is itself an int. This dict used to whitelist only
              # kind/value/x/y, silently dropping dx/dy off every scroll request, so
              # _do_action always fell through to the keyword branch with val=="" (the
              # wheel handler never sends `value`) → amt defaulted to 600 (down) no
              # matter which way the wheel actually moved. That's why wheel-up did
              # nothing while wheel-down "worked".
              "dx": data.get("dx"), "dy": data.get("dy"),
              # drag endpoints (kind=="drag") — were silently dropped by this
              # whitelist (same class as the dx/dy bug above), so a user drag
              # collapsed to (0,0)→(0,0). Pass them through.
              "x0": data.get("x0", 0), "y0": data.get("y0", 0),
              "x1": data.get("x1", 0), "y1": data.get("y1", 0),
              # count carries the native multi-click detail (1=single, 2=double,
              # 3=triple → word/paragraph selection on the remote page).
              "count": data.get("count")}
    if not action["kind"]:
        return jsonify(ok=False, error="missing action kind"), 400
    return jsonify(_streamer.run_action(action))


# ── Live-session driving ──────────────────────────────────
# Dispatch a task to one of the the host bots' real Discord sessions; the bot
# runs it on the SAME shared Chrome the operator views. The browser actions are
# surfaced via the MCP action-tap (operator-events.ndjson) which every bot's
# playwright-mcp wrapper writes to — so the operator shows "🤖 <bot> · Clicking…"
# + the step trail regardless of which bot is driving. (Reasoning relay = stage 2.)
import json as _json
import os as _os

# The 5 drivers: the host bots that can take the wheel. home_channel = where the
# operator posts the task (the running bot picks it up as a prompt). `key` is the
# bot name the action-tap stamps events with (must match detect_bot()).
DRIVERS = [
    {"key": "claude-a", "label": "claude-a"},
    {"key": "claude-b", "label": "claude-b"},
    {"key": "gpt", "label": "gpt"},
    # gemma drives via the agy runtime (flat Google sub) — agy IS gemma's engine,
    # so there's one pickable entry, not a separate "agy" row.
    {"key": "gemma", "label": "gemma"},
]
_DRIVER_BY_KEY = {d["key"]: d for d in DRIVERS}

_EVENT_LOG = _os.path.expanduser("~/.cache/computer-use/operator-events.ndjson")


def _recent_events(limit: int = 40) -> list:
    """Tail the action-tap event log → recent {bot,action,detail,ts} events."""
    try:
        with open(_EVENT_LOG, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(_json.loads(ln))
            except Exception:
                pass
        return out
    except OSError:
        return []


def _current_driver(window_s: float = 12.0) -> dict | None:
    """The bot whose last browser action was within `window_s` → who's driving now."""
    evs = _recent_events(8)
    if not evs:
        return None
    last = evs[-1]
    if time.time() - last.get("ts", 0) <= window_s:
        # demo: never leak squad bot names to a public visitor -> generic label.
        _b = "assistant" if DEMO else last.get("bot")
        return {"bot": _b, "action": last.get("action"),
                "detail": last.get("detail", "")}
    return None


@bp.route("/operator/drivers")
def operator_drivers():
    """The pickable drivers — the operator runs them headless. In demo mode this is
    a single generic 'gpt' driver (never leak squad bot names to a public visitor)."""
    if DEMO:
        return jsonify(drivers=[{"key": "bot", "label": "bot"}])
    return jsonify(drivers=[{"key": d["key"], "label": d["label"]} for d in DRIVERS])


@bp.route("/operator/dispatch", methods=["POST"])
def operator_dispatch():
    """Start a headless Claude Code agent (as the chosen persona) to do the task
    on the shared Chrome — on the subscription, no Discord, no API key."""
    data = request.get_json(silent=True) or request.form
    bot = (data.get("bot") or "").strip()
    task = (data.get("task") or "").strip()
    if not task:
        return jsonify(ok=False, error="empty task"), 400
    model = (data.get("model") or "").strip()
    effort = (data.get("effort") or "").strip()
    # explicit user intent: clear the manual-close latch and re-check liveness/
    # status (no longer relaunches — see _ensure_chrome_alive, 2026-07-05).
    try:
        _streamer._user_closed = False
        _streamer._ensure_chrome_alive()
        _streamer.ensure_running()
    except Exception:
        pass
    if DEMO:
        # public demo: Sonnet 4.6 via gemma/agy runtime, effort locked to medium.
        # ignore any client-sent bot/model. demo=True strips squad context/identity/tools.
        bot = "gemma"
        model = "Claude Sonnet 4.6 (Thinking)"
        effort = "medium"
        r = operator_agent.runner.start(bot, task, model=model, effort=effort, demo=True)
    else:
        r = operator_agent.runner.start(bot, task, model=model, effort=effort)
    return (jsonify(r), 200) if r.get("ok") else (jsonify(r), 409)


# ── Saved tasks (#30) ──────────────────────────────────────────────────────
# A saved task = a named, re-runnable dispatch bundle (prompt + preferred sites
# + default bot/model/effort + optional start_url). v1: no scheduling, and
# preferred-sites is a prompt HINT not a hard sandbox (both deferred — see the
# handoff spec). Persistence + slug logic live in operator_tasks.py; these routes
# are thin wrappers that, on /run, do exactly what /operator/dispatch does.
# LIVE COCKPIT ONLY — never exposed in the public demo (would leak/enumerate the
# squad's saved tasks + let a visitor run arbitrary stored prompts).


def _task_public(slug: str, t: dict) -> dict:
    """The safe outward shape of a saved task for the UI (slug + the fields the
    dispatch box needs to populate, plus stamps)."""
    return {
        "slug": slug,
        "name": t.get("name", ""),
        "prompt": t.get("prompt", ""),
        "sites": t.get("sites", []),
        "bot": t.get("bot", ""),
        "model": t.get("model", ""),
        "effort": t.get("effort", ""),
        "start_url": t.get("start_url", ""),
        "schedule": t.get("schedule", ""),
        "created": t.get("created"),
        "last_run": t.get("last_run"),
    }


@bp.route("/operator/tasks", methods=["GET", "POST"])
def operator_tasks():
    """GET → list saved tasks. POST → create/update (body = data-model fields);
    validates non-empty name+prompt; returns the slug."""
    if DEMO:
        return jsonify(ok=False, error="not available"), 404
    if request.method == "GET":
        tasks = operator_tasks_store.load_tasks()
        items = [_task_public(s, t) for s, t in sorted(tasks.items())]
        return jsonify(ok=True, tasks=items)
    # POST create/update
    data = request.get_json(silent=True) or request.form
    slug, err = operator_tasks_store.save_task({
        "slug": (data.get("slug") or "").strip(),
        "name": data.get("name"),
        "prompt": data.get("task") or data.get("prompt"),
        "sites": data.get("sites"),
        "bot": data.get("bot"),
        "model": data.get("model"),
        "effort": data.get("effort"),
        "start_url": data.get("start_url"),
        "schedule": data.get("schedule"),
    })
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, slug=slug)


@bp.route("/operator/tasks/<slug>/run", methods=["POST"])
def operator_task_run(slug):
    """Load a saved task and dispatch it — mirrors /operator/dispatch exactly,
    plus: optional nav to start_url first, and a preferred-sites prompt preamble.
    Stamps last_run. Body may override bot/model/effort (the UI's editable path);
    absent overrides fall back to the task's stored defaults."""
    if DEMO:
        return jsonify(ok=False, error="not available"), 404
    data = request.get_json(silent=True) or request.form or {}
    r, status = _dispatch_saved_task(slug, data)
    return jsonify(r), status


def _dispatch_saved_task(slug: str, overrides: dict | None = None) -> tuple[dict, int]:
    """The shared saved-task dispatch path — the ▶ run route and the scheduler
    (operator_schedule) both come through here."""
    overrides = overrides or {}
    t = operator_tasks_store.get_task(slug)
    if not t:
        return {"ok": False, "error": "no such task"}, 404
    bot = (overrides.get("bot") or t.get("bot") or "").strip()
    model = (overrides.get("model") or t.get("model") or "").strip()
    effort = (overrides.get("effort") or t.get("effort") or "").strip()
    prompt = (t.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "task has empty prompt"}, 400

    # Same Chrome-ensure as /dispatch: no longer relaunches, just re-checks status.
    try:
        _streamer._user_closed = False
        _streamer._ensure_chrome_alive()
        _streamer.ensure_running()
    except Exception:
        pass

    # Optional: navigate to the task's start_url before handing off to the agent.
    start_url = (t.get("start_url") or "").strip()
    if start_url:
        try:
            _streamer.run_action({"kind": "goto", "value": start_url})
        except Exception:
            pass

    # v1 preferred-sites = prompt hint (not a hard sandbox).
    preamble = operator_tasks_store.sites_preamble(t.get("sites", []))
    task_prompt = f"{preamble}{prompt}" if preamble else prompt

    r = operator_agent.runner.start(bot, task_prompt, model=model, effort=effort)
    if r.get("ok"):
        operator_tasks_store.mark_run(slug)
        return r, 200
    return r, 409


@bp.route("/operator/tasks/<slug>", methods=["DELETE"])
def operator_task_delete(slug):
    """Remove a saved task."""
    if DEMO:
        return jsonify(ok=False, error="not available"), 404
    return jsonify(ok=operator_tasks_store.delete_task(slug))



@bp.route("/operator/agent")
def operator_agent_state():
    """The running agent's reasoning/replies since `since` epoch (for the chat)."""
    try:
        since = float(request.args.get("since", "0") or 0)
    except (TypeError, ValueError):
        since = 0.0
    return jsonify(operator_agent.runner.snapshot(since))


@bp.route("/operator/agent/stop", methods=["POST"])
def operator_agent_stop():
    return jsonify(operator_agent.runner.stop())


@bp.route("/operator/agent/reset", methods=["POST"])
def operator_agent_reset():
    """Clear the agent's conversation memory (wired to the operator trash button)."""
    bot = (request.get_json(silent=True) or {}).get("bot", "")
    return jsonify(operator_agent.runner.reset_session(bot))


@bp.route("/operator/driver-status")
def operator_driver_status():
    """Who's driving + recent bot-action trail (tap log) + the driver's reasoning
    (transcript tail) newer than the client's  epoch."""
    drv = _current_driver()
    try:
        since = float(request.args.get("since", "0") or 0)
    except (TypeError, ValueError):
        since = 0.0
    reasoning = []
    bot = (request.args.get("bot") or (drv or {}).get("bot") or "").strip()
    # in demo mode the agent has no squad transcript to tail (and we must not read
    # any squad bot's transcript) -> the live trace comes from the agent runner only.
    if bot and not DEMO:
        reasoning = _tail_reasoning(bot, since)
    return jsonify(driver=drv, events=_recent_events(30), reasoning=reasoning)


# ── Stage 2: reasoning relay ──────────────────────────────
# Tail the driving bot's live session transcript JSONL → surface its assistant
# text (its reasoning/replies) so the operator chat shows thinking, not just
# clicks. Per-bot transcript dir = <config_dir>/projects/<cwd-slug>/; we take the
# most-recently-modified .jsonl there (the live session).
import glob as _glob

# bot → (config_dir, cwd) used to locate its transcript project dir.
_BOT_PROJECT = {
    "claude-a":     ("~/.claude",            "~/agents/claude-a"),
    "claude-d":  ("~/.claude",            "~/agents/claude-d"),
    "claude-e": ("~/.claude",            "~/agents/claude-e"),
    "claude-b":      ("~/.config/claude-b",        "~"),
    "gpt":        (None, None),  # different arch; no claude transcript
}


def _slug(path: str) -> str:
    """Claude's project-dir slug: the abspath with /._ → -."""
    ap = _os.path.abspath(_os.path.expanduser(path))
    return ap.replace("/", "-").replace("_", "-").replace(".", "-")


def _transcript_file(bot: str) -> str | None:
    """Newest .jsonl for this bot's live session, or None."""
    cfg_cwd = _BOT_PROJECT.get(bot)
    if not cfg_cwd or not cfg_cwd[0]:
        return None
    cfg, cwd = cfg_cwd
    d = _os.path.join(_os.path.expanduser(cfg), "projects", _slug(cwd))
    cands = _glob.glob(_os.path.join(d, "*.jsonl"))
    if not cands:
        # fallback: newest jsonl anywhere under this config's projects
        cands = _glob.glob(_os.path.join(_os.path.expanduser(cfg), "projects", "*", "*.jsonl"))
    if not cands:
        return None
    return max(cands, key=lambda f: _os.path.getmtime(f))


def _assistant_text(msg: dict) -> str:
    """Extract plain assistant text from a transcript line's message.content."""
    m = msg.get("message") or {}
    content = m.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(p for p in parts if p).strip()
    return ""


def _tail_reasoning(bot: str, since_ts: float, limit: int = 8) -> list:
    """Return up to `limit` recent assistant messages (text) newer than since_ts,
    as {text, ts}. Best-effort; never raises."""
    f = _transcript_file(bot)
    if not f:
        return []
    out = []
    try:
        # read only the tail for cheapness
        with open(f, encoding="utf-8") as fh:
            lines = fh.readlines()[-120:]
        for ln in lines:
            try:
                d = _json.loads(ln)
            except Exception:
                continue
            if d.get("type") != "assistant":
                continue
            ts = d.get("timestamp")
            # timestamp is ISO; convert to epoch for comparison
            ep = _iso_epoch(ts)
            if ep <= since_ts:
                continue
            txt = _assistant_text(d)
            if txt:
                out.append({"text": txt[:400], "ts": ep})
    except OSError:
        return []
    return out[-limit:]


def _iso_epoch(ts) -> float:
    """ISO-8601 string → epoch seconds; 0 on failure."""
    if not ts:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


import subprocess as _sp

# bot → marker to find its running session. cwd match for the agent-dir bots;
# claude-b runs from ~ so match its CLAUDE_CONFIG_DIR in the environ instead.
_BOT_LIVE_CWD = {
    "claude-a": "/agents/claude-a",
    "claude-d": "/agents/claude-d",
    "claude-e": "/agents/claude-e",
}
_BOT_LIVE_ENV = {"claude-b": ".claude-alt"}


def _live_bots() -> set:
    """Which driver bots have a running `claude --channels` session right now."""
    live = set()
    try:
        out = _sp.run(["pgrep", "-f", "claude --channels"], capture_output=True,
                      text=True, timeout=6, stdin=_sp.DEVNULL).stdout
        pids = [x for x in out.split() if x.isdigit()]
        for pid in pids:
            try:
                cwd = _os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                cwd = ""
            for bot, marker in _BOT_LIVE_CWD.items():
                if marker in cwd:
                    live.add(bot)
            # claude-b: check environ for its config dir
            if _BOT_LIVE_ENV:
                try:
                    with open(f"/proc/{pid}/environ", "rb") as fh:
                        env = fh.read().decode("utf-8", "ignore")
                    for bot, mk in _BOT_LIVE_ENV.items():
                        if mk in env:
                            live.add(bot)
                except OSError:
                    pass
    except Exception:
        pass
    # gpt is a service bot (always-on if its unit is active) — but it can't drive
    # reliably (one MCP slot, the data service), so we don't mark it live for driving.
    return live


# Model picker options. The VALUE is the alias (opus/sonnet/haiku) — claude
# resolves an alias to the *latest* of that family, so the actual model the agent
# runs is always current. The LABEL is the human version; bump these two lines
# when a family's latest version changes (the only manual touch-point).
OPERATOR_MODELS = [
    {"value": "opus", "label": "Opus 4.8"},
    {"value": "claude-sonnet-5", "label": "Sonnet 5"},
    {"value": "haiku", "label": "Haiku 4.5"},
]
# codex/gpt models (default gpt-5.5 medium).
OPERATOR_MODELS_GPT = [
    {"value": "gpt-5.5", "label": "GPT-5.5"},
    {"value": "gpt-5.4", "label": "GPT-5.4"},
]
# gemma drives via agy (Antigravity) — exposes the full agy model lineup on the owner's
# flat Google sub. Gemini families use the effort picker for tier; the Claude/GPT-OSS
# ones have a fixed tier baked in (no effort). start() folds family+effort into the
# agy --model display string, e.g. "Gemini 3.5 Flash (High)".
OPERATOR_MODELS_GEMMA = [
    {"value": "Gemini 3.5 Flash", "label": "3.5 Flash"},
    {"value": "Claude Sonnet 4.6 (Thinking)", "label": "Sonnet 4.6"},
    {"value": "Claude Opus 4.6 (Thinking)", "label": "Opus 4.6"},
    {"value": "GPT-OSS 120B (Medium)", "label": "GPT-OSS 120B"},
]


# public demo: single model preset (Sonnet via gemma/agy runtime).
OPERATOR_MODELS_DEMO = [
    {"value": "Claude Sonnet 4.6 (Thinking)", "label": "Sonnet 4.6"},
]


@bp.route("/operator/models")
def operator_models():
    if DEMO:
        return jsonify(models=OPERATOR_MODELS_DEMO)
    driver = request.args.get("driver", "")
    if driver == "gpt":
        return jsonify(models=OPERATOR_MODELS_GPT)
    if driver == "gemma":
        return jsonify(models=OPERATOR_MODELS_GEMMA)
    return jsonify(models=OPERATOR_MODELS)


# ── background housekeeping (#2 scheduled tasks + #3 completion pings) ────────
# Started at import (the server imports this module once); the thread is a
# daemon and a no-op when OPERATOR_SCHEDULER=0. Never in the demo — a public
# instance must not fire stored prompts on a clock.
if not DEMO:
    try:
        import operator_schedule as _op_sched
        _op_sched.start(run_fn=lambda slug: _dispatch_saved_task(slug)[0],
                        runner=operator_agent.runner)
    except Exception:  # noqa: BLE001 — housekeeping must never block the app
        pass
