"""Browser operator — live view + full remote control of the logged-in Chrome.

One self-contained surface (full-screen on an iPad over Tailscale) that shows the
real Chrome the app computer-use drives and lets you take the wheel live —
click, type, navigate — interleaving freely with whatever a bot is doing in the
same browser (shared mouse; last action wins). "See it, steer it." 

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

from flask import Blueprint, Response, jsonify, render_template, request, send_file
import operator_agent  # the headless-claude agent runner (option 1)
import operator_tasks as operator_tasks_store  # saved-task store (#30)

import os as _os_cfg
# DEMO isolation the public demo: a second instance runs with OPERATOR_DEMO=1 and
# its own isolated, NOT-logged-in Chrome on a separate CDP port. These env vars are
# unset for the owner live cockpit (-> no behavior change); set only by demo_server.py.
DEMO = _os_cfg.environ.get("OPERATOR_DEMO") == "1"
# both the live _Streamer and the agent MCP attach here in demo mode (isolated
# Chrome), never :9222 (the logged-in browser). The unguessable path gate is the
# WSGI url-prefix mounted by demo_server.py (APPLICATION_ROOT=/<slug>/<hash>).
CDP_URL = _os_cfg.environ.get("OPERATOR_DEMO_CDP") or "http://127.0.0.1:9222"
if DEMO:
    # the demo may view/drive the SANDBOX surface, but never the owner container —
    # scope it to its own (sandbox_container.py reads this at load).
    _os_cfg.environ.setdefault("OPERATOR_SANDBOX_CONTAINER", "operator-sandbox-demo")
FRAME_INTERVAL = 0.066     # ~15fps 
JPEG_QUALITY = 60
IDLE_STOP_AFTER = 90.0
# F1 adaptive frame tier — ?tier=lo (narrow viewport / Save-Data clients) gets
# lean frames. Browser lo frames are downscaled PER-CAPTURE via CDP clip+scale
# (never Emulation.setDeviceMetricsOverride, which would resize the SHARED page
# under the agent) and compressed harder; a Retina tablet otherwise pulls the
# full device-resolution JPEG every frame. Sandbox lo lowers the ffmpeg rate
# and raises -q:v (2-31, higher = smaller frames; hi keeps the 10fps/q8 default).
TIER_LO_QUALITY = 35
TIER_LO_MAX_W = 900
TIER_LO_SANDBOX_FPS, TIER_LO_SANDBOX_Q = 6, 12

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
    _user_closed = False  # True when Chrome was closed manually → don't auto-relaunch 
    _key_repeat = None   # dict[key -> asyncio.Task] — held-key auto-repeat loops
    # F1: frame tier, set by the feed routes (last-viewer-wins on the shared
    # frame buffer — single-user cockpit; per-viewer buffers are a 1.0.10 idea)
    tier: str = "hi"
    _eager_evt = None    # asyncio.Event — an input action wakes the grab loop (F2)
    # relaunch pacing (2026-07-11): with CDP unreachable, every /frame poll used
    # to relaunch the thread — a fresh driver process per second, and any driver
    # not stopped on the failure path leaked (15 orphans in one night). Error
    # exits now arm a growing backoff that ensure_running honors; a good frame
    # resets it so recovery after Chrome comes back is fast again.
    _fail_streak: int = 0
    _backoff_until: float = 0.0
    _was_busy: bool = False   # agent-run edge detector (sweep emulation on end)

    # ---- lifecycle -------------------------------------------------------
    def ensure_running(self) -> None:
        with self._lock:
            self.last_view = time.monotonic()
            # restart if flagged running but the thread actually died (stale flag)
            alive = self._thread is not None and self._thread.is_alive()
            if self._running and alive:
                return
            if time.monotonic() < self._backoff_until:
                return   # recent abnormal death — don't thrash the relaunch
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
            with self._lock:
                if self.status == "error":
                    # abnormal death (attach failure / wedge) → pace the relaunch
                    self._fail_streak = min(self._fail_streak + 1, 8)
                    self._backoff_until = time.monotonic() + min(
                        10.0, 2.0 ** self._fail_streak)
                elif self.status == "live":
                    self.status = "idle"

    @staticmethod
    def _chrome_attach_script() -> str:
        """Path to the (re)launcher for the active mode — the demo's isolated
        headless Chrome under DEMO, the owner logged-in GUI Chrome otherwise."""
        import os
        if DEMO:
            return os.path.expanduser(os.environ.get("OPERATOR_DEMO_CHROME_SCRIPT", "~/.operator-sandbox/op-demo-chrome.sh"))
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
        if pages:
            self._page = pages[0]
        else:
            # fallback page: navigate it to the landing URL — a bare new_page()
            # sits on about:blank forever ("new tab doesn't load the home page")
            self._page = await ctx.new_page()
            self._cdp = None
            try:
                await self._cdp_navigate(self._page, _NEWTAB_DATA_URL)
            except Exception:  # noqa: BLE001 — landing nav is best-effort
                pass
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
        # try/finally so the driver is stopped on EVERY exit path — including an
        # _attach() that raises (2026-07-11: a failed connect_over_cdp left the
        # freshly-started driver orphaned; combined with per-poll relaunch this
        # leaked one node process per second all night).
        try:
            await self._attach()
            await self._grab_loop_inner()
        finally:
            self.frame = None      # stopping → no stale 'live' with no frames
            await self._teardown()

    async def _grab_loop_inner(self) -> None:
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
                    if self._fail_streak:
                        # frames flowing again → recovery is proven; relaunches
                        # go back to instant for the next incident
                        with self._lock:
                            self._fail_streak = 0
                            self._backoff_until = 0.0
                else:
                    _misses += 1
                    if _misses >= 4:
                        # wedged Chrome (alive but screenshots hang/fail). No more
                        # auto-relaunch here (2026-07-05 — see _ensure_chrome_alive):
                        # stop cleanly and surface it instead of shelling out to
                        # chrome-attach.sh, which could race a concurrent dispatch's
                        # own relaunch attempt. Manual relaunch via the bot-chrome
                        # script is the expected recovery now; this is rare enough
                        # (per the owner) that it doesn't need to self-heal.
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
                    try:
                        await self._teardown()
                        self.status = "connecting"   # after teardown (which idles)
                        await self._attach()
                    except Exception as e2:  # noqa: BLE001
                        # browser connection is GONE — exit and let ensure_running
                        # relaunch under the backoff, instead of starting a fresh
                        # driver process inside the loop every ~1.3s (2026-07-11)
                        self.status, self.detail = "error", str(e2)
                        break
            # ease off while an agent drives (shares CDP with the agent's MCP);
            # _pace (not sleep) so a cockpit action wakes the loop instantly (F2)
            try:
                busy = operator_agent.runner.is_running()
            except Exception:
                busy = False
            if self._was_busy and not busy:
                # run just ended → sweep the emulation it may have left on the
                # shared browser (never mid-run: a run may emulate deliberately)
                try:
                    await self._clear_emulation()
                except Exception:  # noqa: BLE001
                    pass
            self._was_busy = busy
            await self._pace(0.45 if busy else FRAME_INTERVAL)

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
            lo = self.tier == "lo"
            args = {"format": "jpeg",
                    "quality": TIER_LO_QUALITY if lo else JPEG_QUALITY}
            if lo and self.vw > TIER_LO_MAX_W and self.vh:
                # F1: downscale per-capture (clip covers the viewport, scale caps
                # the output width) — a page-level emulation override would resize
                # the SHARED page under the agent. No clip when the viewport is
                # unknown (nothing sane to scale against) or already narrow.
                args["clip"] = {"x": 0, "y": 0,
                                "width": float(self.vw), "height": float(self.vh),
                                "scale": TIER_LO_MAX_W / float(self.vw)}
            res = await asyncio.wait_for(
                sess.send("Page.captureScreenshot", args),
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
                    page.screenshot(
                        type="jpeg",
                        quality=TIER_LO_QUALITY if self.tier == "lo" else JPEG_QUALITY,
                        animations="disabled"),
                    timeout=2.5)
            except Exception:
                return None

    async def _pace(self, interval: float) -> None:
        """Sleep the capture interval, but wake IMMEDIATELY when an input action
        lands (F2): the interesting pixels appear in the first ~100ms after a
        click/keypress, and a fixed cadence could sit out a full interval before
        showing them. Idle cadence is untouched — the event only fires on actions."""
        if self._eager_evt is None:
            self._eager_evt = asyncio.Event()
        try:
            await asyncio.wait_for(self._eager_evt.wait(), timeout=interval)
        except asyncio.TimeoutError:
            return
        self._eager_evt.clear()

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
        tab 1) left the view frozen on the stale tab . Here we poll
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
            # ACTIVITY BEATS VISIBILITY : agents drive
            # pages over CDP, which never foregrounds them — the MCP picks its
            # current tab at connect independent of Chrome's focus, and navigate/
            # click never activate a target (only the explicit tab tools do). So
            # a tab whose URL changed since the last poll is the one being DRIVEN;
            # follow it and bring it to front (which also keeps its renderer from
            # being background-throttled and makes later visibility polls agree).
            # ONLY while an agent run is live: outside a run, "URL activity" is
            # SPA churn in idle tabs (Google Travel pushStates on its own), and
            # yanking focus then kills in-page popups the USER is working with
            # (a password manager's inline menu dies on blur) — and in manual mode a view
            # switch would re-aim the user's steer clicks at the wrong page.
            urls = {pg: pg.url for pg in live}
            prev = getattr(self, "_tab_urls", {})
            self._tab_urls = urls
            try:
                _busy = operator_agent.runner.is_running()
            except Exception:  # noqa: BLE001
                _busy = False
            moved = ([pg for pg in live if pg in prev and prev[pg] != urls[pg]]
                     if _busy else [])
            if moved:
                pg = moved[-1]                       # most recently registered mover
                if pg is not self._page:
                    self._page = pg
                    self._cdp = None
                    self._update_viewport()
                try:
                    await asyncio.wait_for(pg.bring_to_front(), timeout=0.5)
                except Exception:  # noqa: BLE001 — foregrounding is best-effort
                    pass
                return
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
        # an error status (wedge, attach failure) must SURVIVE teardown — it
        # carries the user-facing message and keys the relaunch backoff
        if self.status != "error":
            self.status = "idle"

    async def _clear_emulation(self) -> dict:
        """Strip device-metrics + touch emulation overrides from EVERY page of
        the attached browser. Agent MCP sessions (and one-off browser_resize
        calls) leave CDP emulation on the real Chrome, and it OUTLIVES the
        client that set it: pages stay reflowed to the emulated size (the
        2026-07-10 "zoom spaz") and touch emulation kills wheel scrolling.
        Per-page best-effort; a page with no override is a harmless no-op."""
        cleared, failed = 0, 0
        try:
            ctx = self._browser.contexts[0]
            pages = [p for p in ctx.pages if not p.is_closed()]
        except Exception as e:  # noqa: BLE001 — browser gone/never attached
            return {"ok": False, "error": str(e), "cleared": 0, "failed": 0}
        for pg in pages:
            try:
                sess = await ctx.new_cdp_session(pg)
                await asyncio.wait_for(
                    sess.send("Emulation.clearDeviceMetricsOverride"), timeout=1.5)
                await asyncio.wait_for(
                    sess.send("Emulation.setTouchEmulationEnabled",
                              {"enabled": False}), timeout=1.5)
                try:
                    await sess.detach()
                except Exception:  # noqa: BLE001
                    pass
                cleared += 1
            except Exception:  # noqa: BLE001 — dead/privileged page, keep sweeping
                failed += 1
        return {"ok": True, "cleared": cleared, "failed": failed}

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
            elif kind == "move":                   # hover at normalized x,y (menus, tooltips)
                dims = await self._viewport_dims(p)
                x = float(action.get("x", 0)) * dims["w"]
                y = float(action.get("y", 0)) * dims["h"]
                sess = await self._cdp_session(p)
                await asyncio.wait_for(
                    sess.send("Input.dispatchMouseEvent",
                              {"type": "mouseMoved", "x": x, "y": y}), timeout=4)
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
                # back/forward . The feed then streams the new page as it loads.
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
            elif kind == "reset_view":
                # strip emulation overrides an agent run (or a stray
                # browser_resize) left on the shared browser — the "stuck
                # phone-zoom + dead scrolling" recovery, one tap from the menu
                res = await self._clear_emulation()
                return res
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
            # F2: whatever the action did, paint its result on the next grab —
            # running on the streamer loop, so touching the Event here is safe.
            if self._eager_evt is None:
                self._eager_evt = asyncio.Event()
            self._eager_evt.set()

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


# ── Track C: surfaces (browser / desktop-sandbox / desktop-real) ─────────────
# The active surface decides the live-feed source and what the dispatched agent
# drives. Module-level state, reset to browser on server restart (safe default).
import shutil as _shutil
import subprocess as _fsp

_CU_DIR = str(Path(__file__).resolve().parent.parent / "computer-use")

_SURFACE_DEFS = [
    {"key": "browser", "label": "Browser",
     "hint": "Controls a Chrome browser."},
    {"key": "desktop-sandbox", "label": "Sandbox",
     "hint": "Controls an isolated virtual desktop."},
    {"key": "desktop-real", "label": "Computer", "gated": True,
     "hint": "Controls this machine directly. Requires confirmation."},
]
_active_surface = {"name": "browser"}


def _surface_available(key: str) -> bool:
    if key == "browser":
        return True
    if key == "desktop-sandbox":
        # the sandbox is a Docker container now (sandbox_container.py), not the
        # old host-Xvfb path — Xvfb/scrot live INSIDE the image.
        return bool(_shutil.which("docker"))
    if key == "desktop-real":
        # bare which() fails under the systemd --user unit (interop dirs not on
        # its PATH) — accept the canonical WSL interop path too, matching the
        # resolution win_backend.py does.
        _ps = (_shutil.which("powershell.exe")
               or ("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
                   if _os_cfg.path.exists(
                       "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
                   else None))
        return bool(_ps and _os_cfg.path.exists(
            _os_cfg.path.join(_CU_DIR, "win_capture.ps1")))
    return False


_CU_CACHE: dict = {}


def _load_cu(fname: str):
    """Import a computer-use module by path (dir name has a dash). CACHED —
    sandbox_container holds live pipe state (the persistent xdotool shell), and
    a fresh exec_module per steer action re-created its docker exec every time:
    ~130ms/action instead of ~10ms through the warm pipe."""
    if fname not in _CU_CACHE:
        import importlib.util
        p = _os_cfg.path.join(_CU_DIR, fname)
        spec = importlib.util.spec_from_file_location("cu_feed_" + fname[:-3], p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CU_CACHE[fname] = mod
    return _CU_CACHE[fname]


class _DesktopFeed:
    """Live-feed source for the desktop surfaces — the desktop counterpart of
    _Streamer. Captures via the same computer-use backends the agent drives
    (scrot on the Xvfb display / win_capture.ps1 on the real desktop), at a
    gentler cadence (full-desktop captures are heavier than CDP frames).
    Serves PNG or JPEG parts; the stream generator reads .mime per frame."""

    def __init__(self) -> None:
        self.frame: bytes | None = None
        self.frame_ts: float = 0.0
        self.mime: str = "image/jpeg"
        self.last_view: float = 0.0
        self.detail: str = ""
        self.surface: str = "desktop-sandbox"
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._mods: dict = {}
        self._stream_dead_until = 0.0   # cooldown after an ffmpeg-stream failure
        self.tier: str = "hi"           # F1: set by the feed routes (last-viewer-wins)
        self._wake = threading.Event()  # F2: a steer poke wakes the capture loop

    def _pace(self, interval: float) -> None:
        """Sleep between captures, but wake immediately on a steer poke (F2).
        Consume-once so the idle cadence stays untouched afterward."""
        if self._wake.wait(timeout=interval):
            self._wake.clear()

    def ensure_running(self, surface: str) -> None:
        with self._lock:
            self.last_view = time.monotonic()
            self.surface = surface
            alive = self._thread is not None and self._thread.is_alive()
            if self._running and alive:
                return
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="operator-desktop-feed")
            self._thread.start()

    def _run(self) -> None:
        while self._running:
            if time.monotonic() - self.last_view > IDLE_STOP_AFTER:
                break
            # sandbox: prefer the persistent MJPEG stream (~8fps, one exec for
            # its whole life) over per-frame scrot execs (~1fps, laggy)
            if (self.surface == "desktop-sandbox"
                    and time.monotonic() >= self._stream_dead_until
                    and self._stream()):
                continue      # stream ended (switch/pipe death) — re-decide
            try:
                path = self._capture()
                if path:
                    with open(path, "rb") as f:
                        data = f.read()
                    # scrot writes PNG regardless of suffix; sniff the magic
                    self.mime = ("image/png" if data[:8] == b"\x89PNG\r\n\x1a\n"
                                 else "image/jpeg")
                    self.frame = data
                    self.frame_ts = time.monotonic()
                    self.detail = ""
            except Exception as e:  # noqa: BLE001 — feed must idle, not die
                self.detail = str(e)
            self._pace(1.2 if self.surface == "desktop-real" else 0.6)
        self.frame = None
        self._running = False

    # §feed-decay (2026-07-09): a long-lived x11grab stream DECAYS on this Xvfb —
    # young streams deliver 10fps with a 20-80ms action→visible latency; by ~8min
    # of age the same pipeline was measured ~2s stale (the "works great after a
    # surface flip, shits the bed a minute later" cockpit lag). Rather than chase
    # ffmpeg's internals, the reader self-heals: count frames actually received
    # per window and CYCLE the stream (kill + immediate respawn, last frame kept,
    # ~0.5s blip) whenever the rate sags below the floor. MJPEG emits ~10 frames/s
    # even on a static screen (identical bytes are still frames), so a sagging
    # receive rate means pipeline decay, not a quiet desktop.
    _HEALTH_WINDOW_S = 5.0    # measure received-fps over this window
    _HEALTH_MIN_FPS = 4.0     # below this → cycle (configured rate is 10)
    _HEALTH_GRACE_S = 15.0    # never judge a freshly-spawned stream

    @classmethod
    def _stream_decayed(cls, n_frames: int, window_s: float, age_s: float) -> bool:
        """Pure decision: has the stream's delivery rate sagged enough to cycle?"""
        if age_s < cls._HEALTH_GRACE_S or window_s < cls._HEALTH_WINDOW_S:
            return False
        return (n_frames / window_s) < cls._HEALTH_MIN_FPS

    def _stream(self) -> bool:
        """Read the sandbox's long-lived ffmpeg MJPEG pipe until the surface
        changes, the viewer idles out, the pipe dies, or delivery decays (see
        _stream_decayed above). Returns True if the stream ran; False → fall
        back to scrot polling (with a cooldown, so an image without ffmpeg
        doesn't pay the spawn cost on every frame)."""
        if "sandbox" not in self._mods:
            self._mods["sandbox"] = _load_cu("sandbox_container.py")
        sb = self._mods["sandbox"]
        # F1: the tier picks the ffmpeg rate/quality at spawn; a mid-stream tier
        # change breaks the read loop below so the outer loop respawns with the
        # new params (~0.5s blip, same path as the decay cycle).
        spawn_tier = self.tier
        fps, q = ((TIER_LO_SANDBOX_FPS, TIER_LO_SANDBOX_Q)
                  if spawn_tier == "lo" else (10, 8))
        try:
            proc = sb.open_stream(fps=fps, quality=q)
        except Exception as e:  # noqa: BLE001 — ffmpeg missing / container down
            self.detail = f"starting sandbox desktop… ({e})"
            self._stream_dead_until = time.monotonic() + 45
            return False
        tail = b""
        born = win_t = time.monotonic()
        win_n = 0
        try:
            while (self._running and self.surface == "desktop-sandbox"
                   and self.tier == spawn_tier
                   and time.monotonic() - self.last_view <= IDLE_STOP_AFTER):
                # read1: return as soon as ANY bytes arrive. A plain read(64KB)
                # blocks until the full 64KB accumulates (~2 frames at q8), so
                # it surfaced only every other frame and added ~400ms of
                # chunk-accumulation latency (halved the configured 10fps).
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break                    # container gone — outer loop re-decides
                frames, tail = sb.split_jpegs(tail + chunk)
                if frames:
                    win_n += len(frames)
                    self.mime = "image/jpeg"
                    self.frame = frames[-1]
                    self.frame_ts = time.monotonic()
                    self.detail = ""
                now = time.monotonic()
                if now - win_t >= self._HEALTH_WINDOW_S:
                    if self._stream_decayed(win_n, now - win_t, now - born):
                        break   # cycle: finally reaps, outer loop respawns fresh
                    win_t, win_n = now, 0
        finally:
            # stop_stream kills BOTH the host exec client AND the container-side
            # ffmpeg — a bare proc.kill() leaves the in-container ffmpeg orphaned,
            # still grabbing X11; those stacked up and made the feed lag worse the
            # longer a session ran (2026-07-09).
            try:
                sb.stop_stream(proc)
            except Exception:  # noqa: BLE001
                pass
        return True

    def _capture(self) -> str | None:
        if self.surface == "desktop-real":
            if "win" not in self._mods:
                self._mods["win"] = _load_cu("win_backend.py")
            return self._mods["win"].screenshot("windows-primary", _SHOT_DIR)
        # sandbox: a REAL isolated Docker desktop. Bring the container up (once)
        # and capture it via docker exec. ensure() is idempotent + persistent —
        # the container survives across switches; it is never torn down here.
        if "sandbox" not in self._mods:
            self._mods["sandbox"] = _load_cu("sandbox_container.py")
        try:
            self._mods["sandbox"].ensure()
        except Exception as e:  # noqa: BLE001 — surface the reason, keep idling
            self.detail = f"starting sandbox desktop… ({e})"
            return None
        return self._mods["sandbox"].screenshot(_SHOT_DIR)


_desktop_feed = _DesktopFeed()


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
    # demo: serve the standalone, de-PII'd template (no the app chrome/nav, no owner
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


def _apply_feed_tier() -> None:
    """F1: read ?tier=lo|hi off the request and stamp BOTH feed sources.
    Anything but 'lo' is 'hi'. Last-viewer-wins on the shared frame buffer —
    fine for a single-user cockpit (per-viewer buffers are a 1.0.10 idea)."""
    tier = "lo" if request.args.get("tier") == "lo" else "hi"
    _streamer.tier = tier
    _desktop_feed.tier = tier


@bp.route("/operator/stream")
def operator_stream():
    """MJPEG multipart stream — renders into an <img>. Survives frame gaps.
    Source-switched per frame by the active surface: browser → the CDP
    _Streamer; desktop surfaces → the _DesktopFeed. Switching surfaces mid-
    stream just swaps the source, no reconnect needed."""
    _apply_feed_tier()
    if _active_surface["name"] == "browser":
        _streamer.ensure_running()
    else:
        _desktop_feed.ensure_running(_active_surface["name"])

    def _part(data, mime=b"image/jpeg"):
        return (b"--frame\r\n"
                b"Content-Type: " + mime + b"\r\n"
                b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                + data + b"\r\n")

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
            if _active_surface["name"] == "browser":
                src = _streamer
                _streamer.ensure_running()
                mime = b"image/jpeg"
            else:
                src = _desktop_feed
                _desktop_feed.ensure_running(_active_surface["name"])
                mime = src.mime.encode()
            src.last_view = time.monotonic()
            f = src.frame
            ts = src.frame_ts
            now = time.monotonic()
            if f and ts != last_sent:
                last_sent = ts; last_push = now      # push a new frame immediately
                yield _part(f, mime)
            elif f and (now - last_push) > 1.0:
                last_push = now                       # ~1s heartbeat of last frame
                yield _part(f, mime)
            elif not f and (now - last_push) > 1.0:
                last_push = now                       # placeholder heartbeat (no frame yet)
                yield _part(_PLACEHOLDER_JPEG)
            time.sleep(POLL)

    resp = Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/operator/frame")
def operator_frame():
    """Single newest frame — the pull half of the feed. The MJPEG push stream
    has no backpressure: a client that decodes slower than the feed produces
    (iPad Safari) buffers the excess and drifts PROGRESSIVELY behind live —
    the 'works great after a reconnect, shits the bed a minute later' lag
    (2026-07-09). The cockpit now self-clocks instead: fetch a frame, render
    it, only then fetch the next — latency is bounded at ~1 frame in flight
    by construction, on any device or link. Fast clients still get ~10fps."""
    _apply_feed_tier()
    if _active_surface["name"] == "browser":
        _streamer.ensure_running()
        src, mime = _streamer, "image/jpeg"
    else:
        _desktop_feed.ensure_running(_active_surface["name"])
        src, mime = _desktop_feed, _desktop_feed.mime
    src.last_view = time.monotonic()
    f = src.frame
    resp = Response(f or _PLACEHOLDER_JPEG,
                    mimetype=mime if f else "image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Operator-Frame"] = "live" if f else "placeholder"
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


# Dirs whose screenshots are served read-only by basename, so a screenshot the
# agent references inline (![](file://.../x.png)) renders in the chat instead
# of being stripped to a text note. The list is owned by operator_trace (1.0.8
# R3) so this route and the trace rewriter can never disagree (a mismatch 404s
# the rewritten links). [0] is the MCP output dir; the rest are per-bot cwds.
from operator_trace import shot_dirs as _shot_dirs
_SHOT_DIRS = _shot_dirs()
_SHOT_DIR = _SHOT_DIRS[0]


def _find_shot(base: str) -> str | None:
    """Locate a screenshot by basename across the servable dirs (first hit)."""
    for d in _SHOT_DIRS:
        target = _os_cfg.path.realpath(_os_cfg.path.join(d, base))
        if _os_cfg.path.commonpath([target, d]) == d and _os_cfg.path.isfile(target):
            return d
    return None


@bp.route("/operator/shot/<path:name>")
def operator_shot(name):
    """Serve an agent screenshot PNG/JPG by basename from the computer-use output
    dir or a bot session dir. Basename-only + extension whitelist + realpath
    containment → no traversal."""
    from flask import send_from_directory, abort
    base = _os_cfg.path.basename(name)            # strip any path components
    if not base or base != name or base.startswith("."):
        abort(404)
    if _os_cfg.path.splitext(base)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        abort(404)
    d = _find_shot(base)
    if d is None:
        abort(404)
    resp = send_from_directory(d, base)
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
    surface = _active_surface["name"]
    if surface != "browser":
        # desktop feed: freshness from ITS buffer; no viewport/url/click mapping
        # (manual control is browser-only — the UI disables it on desktop).
        _desktop_feed.last_view = time.monotonic()
        fresh = (_desktop_feed.frame is not None
                 and (time.monotonic() - _desktop_feed.frame_ts) < 8.0)
        return jsonify(status=("live" if fresh else "connecting"),
                       detail=_desktop_feed.detail, has_frame=fresh,
                       vw=0, vh=0, url="", click=None, surface=surface)
    fresh = (_streamer.frame is not None
             and (time.monotonic() - _streamer.frame_ts) < 6.5)
    cur_url = _streamer.cur_url
    lx, ly, lt = _streamer.last_click
    click = None
    if lt and (time.monotonic() - lt) < 1.2:
        click = {"x": round(lx, 4), "y": round(ly, 4), "age": round(time.monotonic() - lt, 3)}
    return jsonify(status=_streamer.status, detail=_streamer.detail,
                   has_frame=fresh, vw=_streamer.vw, vh=_streamer.vh, url=cur_url,
                   click=click, surface=surface)


@bp.route("/operator/history")
def operator_history_list():
    """Flight-recorder rows, newest first (lean — no trace payloads)."""
    if DEMO:
        return jsonify(ok=False, error="history is live-cockpit only"), 403
    import operator_history as _hist
    try:
        limit = min(max(int(request.args.get("limit", 30)), 1), 200)
    except (TypeError, ValueError):
        limit = 30
    return jsonify(ok=True, runs=_hist.recent(limit))


@bp.route("/operator/history/<int:run_id>")
def operator_history_get(run_id: int):
    """One recorded run, full trace included."""
    if DEMO:
        return jsonify(ok=False, error="history is live-cockpit only"), 403
    import operator_history as _hist
    row = _hist.get(run_id)
    if row is None:
        return jsonify(ok=False, error="no such run"), 404
    return jsonify(ok=True, run=row)


@bp.route("/operator/session", methods=["GET", "POST"])
def operator_session():
    """The ONE shared cockpit session (chat log / mode / picker state), synced
    across devices. GET → {ok, rev, data}; POST {data} → {ok, rev}. The public
    demo is per-visitor by design (localStorage) — hard-gated here."""
    if DEMO:
        return jsonify(ok=False, error="demo sessions are per-visitor"), 403
    import operator_session as _sess_store
    if request.method == "GET":
        got = _sess_store.load()
        return jsonify(ok=True, rev=got["rev"], data=got["data"])
    body = request.get_json(silent=True) or {}
    data = body.get("data")
    if not isinstance(data, dict):
        return jsonify(ok=False, error="body must be {data: {...}}"), 400
    try:
        rev = _sess_store.save(data)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 413
    return jsonify(ok=True, rev=rev)


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


def _img_dims(data: bytes) -> tuple | None:
    """(w, h) from PNG/JPEG header bytes — no PIL dependency in the server."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
        if data[:2] == b"\xff\xd8":                     # JPEG: scan SOF marker
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    except Exception:  # noqa: BLE001
        pass
    return None


def _desktop_steer(action: dict) -> dict:
    """Manual steer for the desktop surfaces — the same normalized-coordinate
    gestures the browser stage sends, injected via the surface's own backend
    (docker-exec xdotool for the sandbox, win input for the real desktop)."""
    surface = _active_surface["name"]
    mod = _load_cu("sandbox_container.py" if surface == "desktop-sandbox"
                   else "win_backend.py")
    # frame size: sandbox is fixed; real desktop = the last streamed frame's
    # dims (win_backend scales image→physical coords itself at exec time)
    if surface == "desktop-sandbox":
        w, h = mod.size()
    else:
        dims = _img_dims(_desktop_feed.frame or b"")
        if not dims:
            return {"ok": False, "error": "no desktop frame yet"}
        w, h = dims

    def _xy(nx, ny):
        return [max(0, min(w - 1, int(float(nx or 0) * w))),
                max(0, min(h - 1, int(float(ny or 0) * h)))]

    def _run(a: dict) -> None:
        if surface == "desktop-sandbox":
            # no ensure() here — that's a docker-inspect subprocess (~80ms) per
            # action; the input pipe already self-heals (its retry calls ensure
            # when the pipe is dead), so a live pipe means a live container.
            mod.execute(a)
        else:
            mod.execute(a, "windows-primary")

    kind = action.get("kind")
    try:
        if kind == "move":
            _run({"action": "mouse_move", "coordinate": _xy(action["x"], action["y"])})
        elif kind in ("click_at", "dblclick_at"):
            n = action.get("count") or (2 if kind == "dblclick_at" else 1)
            act_name = {1: "left_click", 2: "double_click"}.get(
                min(int(n), 3), "triple_click")
            if surface == "desktop-real" and act_name == "triple_click":
                act_name = "double_click"          # win backend caps at double
            _run({"action": act_name, "coordinate": _xy(action["x"], action["y"])})
        elif kind == "rclick_at":
            _run({"action": "right_click", "coordinate": _xy(action["x"], action["y"])})
        elif kind == "drag":
            _run({"action": "left_click_drag",
                  "start_coordinate": _xy(action.get("x0"), action.get("y0")),
                  "coordinate": _xy(action.get("x1"), action.get("y1"))})
        elif kind in ("mousedown_at", "mouseup_at"):
            _run({"action": "mouse_move", "coordinate": _xy(action["x"], action["y"])})
            _run({"action": "left_mouse_down" if kind == "mousedown_at"
                  else "left_mouse_up"})
        elif kind == "scroll":
            dx, dy = action.get("dx"), action.get("dy")
            if isinstance(dy, (int, float)) and dy:
                _run({"action": "scroll",
                      "scroll_direction": "down" if dy > 0 else "up",
                      "scroll_amount": max(1, min(10, round(abs(dy) / 80)))})
            if isinstance(dx, (int, float)) and dx:
                _run({"action": "scroll",
                      "scroll_direction": "right" if dx > 0 else "left",
                      "scroll_amount": max(1, min(10, round(abs(dx) / 80)))})
        elif kind == "type":
            _run({"action": "type", "text": str(action.get("value", ""))})
        elif kind in ("key", "key_down", "key_up"):
            k = kind
            if surface == "desktop-real" and kind != "key":
                # win backend has no keydown/keyup — degrade a hold to one press
                if kind == "key_up":
                    return {"ok": True}
                k = "key"
            _run({"action": k, "text": str(action.get("value", ""))})
        else:
            return {"ok": False, "error": f"{kind!r} not supported on this surface"}
        # F2: the action landed — wake the capture loop so its result paints
        # now instead of up to a full poll interval later (scrot path; the
        # ffmpeg stream is already continuous).
        _desktop_feed._wake.set()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001 — surface the reason to the cockpit
        return {"ok": False, "error": str(e)}


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
              # nothing while wheel-down "worked" .
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
    # desktop surfaces: same gestures, injected via the surface backend instead
    # of CDP — manual steer works everywhere the feed does.
    if _active_surface["name"] != "browser":
        return jsonify(_desktop_steer(action))
    return jsonify(_streamer.run_action(action))


# ── Live-session driving  ──────────────────────────────────
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
        # demo: never leak the app bot names to a public visitor -> generic label.
        _b = "assistant" if DEMO else last.get("bot")
        return {"bot": _b, "action": last.get("action"),
                "detail": last.get("detail", "")}
    return None


@bp.route("/operator/drivers")
def operator_drivers():
    """The pickable drivers — the operator runs them headless. In demo mode this is
    a single generic 'gpt' driver (never leak the app bot names to a public visitor)."""
    if DEMO:
        return jsonify(drivers=[{"key": "bot", "label": "bot"}])
    return jsonify(drivers=[{"key": d["key"], "label": d["label"]} for d in DRIVERS])


@bp.route("/operator/surfaces")
def operator_surfaces():
    """The pickable surfaces (Track C). Demo gets the browser + the (isolated,
    demo-scoped) sandbox; the REAL desktop exposes the host machine, so it shows
    but stays grayed out — live-cockpit only."""
    out = [dict(s, available=_surface_available(s["key"]))
           for s in _SURFACE_DEFS]
    if DEMO:
        for s in out:
            if s["key"] == "desktop-real":
                s["available"] = False
                s["unavailable_hint"] = "Live cockpit only."
        return jsonify(surfaces=out, active=_active_surface["name"])
    return jsonify(surfaces=out, active=_active_surface["name"])


@bp.route("/operator/surface", methods=["POST"])
def operator_surface_set():
    """Switch the active surface: swaps the live-feed source immediately and
    sets the default surface for the next dispatch. desktop-real demands the
    explicit confirm flag every time (the UI shows the consent step)."""
    data = request.get_json(silent=True) or request.form
    name = (data.get("surface") or "").strip()
    if DEMO and name == "desktop-real":
        return jsonify(ok=False, error="the real desktop is live-cockpit only"), 403
    if name not in [s["key"] for s in _SURFACE_DEFS]:
        return jsonify(ok=False, error=f"unknown surface {name!r}"), 400
    if not _surface_available(name):
        return jsonify(ok=False, error=f"{name} not available on this host"), 409
    if name == "desktop-real" and not data.get("confirm"):
        return jsonify(ok=False, error="desktop-real needs confirm"), 403
    _active_surface["name"] = name
    if name != "browser":
        _desktop_feed.ensure_running(name)
    return jsonify(ok=True, active=name)


# Game maps live in vision/maps/ — scanned directly (no heavy import). Selecting
# one only scopes the agent's perceive/game_macro calls; there's no host-side
# "active map" state — the pick is folded into the dispatched task text.
_MAPS_DIR = str(Path(__file__).resolve().parent / "vision" / "maps")


@bp.route("/operator/maps")
def operator_maps():
    """Game maps shippable to perceive/game_macro. Demo never plays games."""
    if DEMO:
        return jsonify(maps=[])
    names = []
    try:
        for f in sorted(_os_cfg.listdir(_MAPS_DIR)):
            base, ext = _os_cfg.path.splitext(f)
            if ext in (".yaml", ".yml", ".json"):
                names.append(base)
    except FileNotFoundError:
        pass
    return jsonify(maps=names)


# apps the taskbar can launch inside the sandbox (whitelist — the route never
# execs a client-supplied binary name).
_SANDBOX_APPS = {"chromium": "Chromium", "xfce4-terminal": "Terminal",
                 "thunar": "Files", "mousepad": "Editor"}


@bp.route("/operator/sandbox/ctl", methods=["POST"])
def operator_sandbox_ctl():
    """Taskbar controls for the sandbox desktop. launch/restart act on the
    persistent container; delete is the ONE destructive teardown — the next
    capture boots a factory-fresh desktop. Demo instances act on their own
    container (OPERATOR_SANDBOX_CONTAINER is demo-scoped at module load),
    so this is safe to expose there too."""
    if not _surface_available("desktop-sandbox"):
        return jsonify(ok=False, error="sandbox not available on this host"), 409
    data = request.get_json(silent=True) or request.form
    act = (data.get("action") or "").strip()
    sb = _load_cu("sandbox_container.py")
    try:
        if act == "launch":
            app_name = (data.get("app") or "").strip()
            if app_name not in _SANDBOX_APPS:
                return jsonify(ok=False, error=f"unknown app {app_name!r}"), 400
            sb.ensure()
            sb.launch(app_name)
        elif act == "restart":
            sb.stop()
            sb.ensure()
        elif act == "delete":
            sb.delete()
        else:
            return jsonify(ok=False, error=f"unknown action {act!r}"), 400
    except Exception as e:  # noqa: BLE001 — surface the reason to the taskbar
        return jsonify(ok=False, error=str(e)), 500
    return jsonify(ok=True)


# ── sandbox file exchange (Transfer) ─────────────────────────────────────────
# In and out of the container's Downloads/Desktop/Documents only; path shape is
# validated by sandbox_container.safe_rel. NEVER in the demo — the demo box is
# shared between strangers, and one visitor must not see another's files.
def _sandbox_files_guard():
    if DEMO:
        return jsonify(ok=False, error="file transfer is live-cockpit only"), 403
    if not _surface_available("desktop-sandbox"):
        return jsonify(ok=False, error="sandbox not available on this host"), 409
    return None


@bp.route("/operator/sandbox/files")
def operator_sandbox_files():
    guard = _sandbox_files_guard()
    if guard:
        return guard
    sb = _load_cu("sandbox_container.py")
    try:
        return jsonify(ok=True, dirs=sb.list_files())
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)), 500


@bp.route("/operator/sandbox/upload", methods=["POST"])
def operator_sandbox_upload():
    guard = _sandbox_files_guard()
    if guard:
        return guard
    sb = _load_cu("sandbox_container.py")
    if (request.content_length or 0) > sb.MAX_FILE_BYTES:
        return jsonify(ok=False, error="file too large"), 413
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="no file"), 400
    import tempfile
    import werkzeug.utils as _wu
    name = _wu.secure_filename(f.filename) or "upload"
    try:
        with tempfile.TemporaryDirectory(dir=_SHOT_DIR) as td:
            tmp = _os.path.join(td, name)
            f.save(tmp)
            rel = sb.put_file(tmp, name)
        return jsonify(ok=True, path=rel)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)), 500


@bp.route("/operator/sandbox/file/<path:rel>")
def operator_sandbox_file(rel: str):
    guard = _sandbox_files_guard()
    if guard:
        return guard
    sb = _load_cu("sandbox_container.py")
    try:
        out = sb.get_file(rel, _os.path.join(_SHOT_DIR, "sandbox-out"))
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)), 400
    return send_file(out, as_attachment=True,
                     download_name=_os.path.basename(out))


def _desktop_real_preflight() -> str | None:
    """Probe the real-desktop capture before a desktop-real run. Returns an
    error string when the console can't actually be seen. A locked or
    non-interactive Windows session reports the disconnected-console default
    geometry (exactly 1024×768) and captures a blank frame — on this panel
    (2816×1940) that geometry is unambiguous. The probe costs one capture
    (~1-2s), paid only on desktop-real dispatches."""
    try:
        wb = _load_cu("win_backend.py")
        w, h = wb.screen_size()
    except Exception as e:  # noqa: BLE001 — no powershell / capture broke
        return f"desktop-real capture unavailable: {e}"
    if (w, h) == (1024, 768):
        return ("the Windows console looks locked or headless (phantom "
                "1024×768 screen) — unlock the desktop, then dispatch again")
    return None


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
    # surface: explicit in the request, else the cockpit's active pick. The
    # runner re-validates (gating is server-side, not a UI courtesy).
    surface = (data.get("surface") or _active_surface["name"] or "browser").strip()
    real_ok = bool(data.get("real_ok"))
    if DEMO:
        # #27: the demo agent may drive the browser or the ISOLATED demo sandbox
        # container — never the real desktop (coerced, no confirm honored).
        if surface not in ("browser", "desktop-sandbox"):
            surface = "browser"
        real_ok = False
    if surface == "desktop-real":
        # pre-flight the capture BEFORE any side effect (surface flip, run
        # start): with the Windows console locked, win_capture returns a
        # phantom blank 1024×768 screen (verified 2026-07-11) and the run
        # would burn itself clicking into a white void.
        err = _desktop_real_preflight()
        if err:
            return jsonify(ok=False, error=err), 409
    if surface != _active_surface["name"] \
            and surface in [s["key"] for s in _SURFACE_DEFS]:
        _active_surface["name"] = surface     # feed follows the dispatch
    # explicit user intent: clear the manual-close latch and re-check liveness/
    # status (no longer relaunches — see _ensure_chrome_alive, 2026-07-05).
    try:
        _streamer._user_closed = False
        _streamer._ensure_chrome_alive()
        _streamer.ensure_running()
    except Exception:
        pass
    if DEMO:
        # public demo: gemma/agy runtime, model locked to the 2-entry demo list
        # (off-list → Flash 3.5 Low default). The tier lives in the model string
        # ("(Thinking)"/"(Low)"), so client-sent effort is discarded — the lock
        # owns effort. demo=True strips the app context/identity/tools.
        bot = "gemma"
        if model not in {m["value"] for m in OPERATOR_MODELS_DEMO}:
            model = OPERATOR_MODELS_DEMO[0]["value"]
        if surface == "desktop-sandbox":
            # Flash has no computer-use tools  — a sandbox run
            # would just shell around. Desktop runs force Sonnet.
            model = "Claude Sonnet 4.6 (Thinking)"
        effort = ""
        r = operator_agent.runner.start(bot, task, model=model, effort=effort,
                                        demo=True, surface=surface)
    else:
        r = operator_agent.runner.start(bot, task, model=model, effort=effort,
                                        surface=surface, real_ok=real_ok)
    return (jsonify(r), 200) if r.get("ok") else (jsonify(r), 409)


# ── Saved tasks (#30) ──────────────────────────────────────────────────────
# A saved task = a named, re-runnable dispatch bundle (prompt + preferred sites
# + default bot/model/effort + optional start_url). v1: no scheduling, and
# preferred-sites is a prompt HINT not a hard sandbox (both deferred — see the
# handoff spec). Persistence + slug logic live in operator_tasks.py; these routes
# are thin wrappers that, on /run, do exactly what /operator/dispatch does.
# DEMO : available, but against a demo-scoped store — the demo
# instance MUST set OPERATOR_TASKS_PATH so visitors never see the app tasks.
# Demo saves strip bot/schedule (forced at run / scheduler never runs in demo),
# the store is capped, and /run applies the same lock as /operator/dispatch.

DEMO_TASKS_MAX = 24     # demo store cap — visitors can't grow the file unboundedly


def _demo_tasks_guard():
    """Fail closed: demo saved tasks REQUIRE the demo-scoped store. If the demo
    launch didn't set OPERATOR_TASKS_PATH, serving these routes would expose the
    owner's real task store — keep the old 404 gate instead."""
    if DEMO and not _os.environ.get("OPERATOR_TASKS_PATH"):
        return jsonify(ok=False, error="not available"), 404
    return None


def _task_public(slug: str, t: dict) -> dict:
    """The safe outward shape of a saved task for the UI (slug + the fields the
    dispatch box needs to populate, plus stamps)."""
    return {
        "slug": slug,
        "name": t.get("name", ""),
        "prompt": t.get("prompt", ""),
        "vars": operator_tasks_store.extract_vars(t.get("prompt", "")),
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
    guard = _demo_tasks_guard()
    if guard:
        return guard
    if request.method == "GET":
        tasks = operator_tasks_store.load_tasks()
        items = [_task_public(s, t) for s, t in sorted(tasks.items())]
        return jsonify(ok=True, tasks=items)
    # POST create/update
    data = request.get_json(silent=True) or request.form
    if DEMO:
        tasks_now = operator_tasks_store.load_tasks()
        if (data.get("slug") or "").strip() not in tasks_now \
                and len(tasks_now) >= DEMO_TASKS_MAX:
            return jsonify(ok=False, error="demo task limit reached"), 400
    slug, err = operator_tasks_store.save_task({
        "slug": (data.get("slug") or "").strip(),
        "name": data.get("name"),
        "prompt": data.get("task") or data.get("prompt"),
        "sites": data.get("sites"),
        # bot/schedule are dead fields in demo: bot is forced at run and the
        # scheduler never starts on a public instance — don't store them.
        "bot": "" if DEMO else data.get("bot"),
        "model": data.get("model"),
        "effort": data.get("effort"),
        "start_url": data.get("start_url"),
        "schedule": "" if DEMO else data.get("schedule"),
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
    guard = _demo_tasks_guard()
    if guard:
        return guard
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
    # {{variables}} (1.0.13): fill from overrides.vars; anything left unfilled
    # bounces with the missing names so the client can collect values (and the
    # scheduler's bare dispatch of a var task fails loudly instead of running
    # a prompt full of literal braces — save_task also refuses that combo).
    if operator_tasks_store.extract_vars(prompt):
        prompt, missing = operator_tasks_store.fill_vars(
            prompt, overrides.get("vars") or {})
        if missing:
            return {"ok": False, "vars": missing,
                    "error": "task needs variable values: "
                             + ", ".join(missing)}, 400

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

    if DEMO:
        # same lock as /operator/dispatch: forced runtime, model allowlist,
        # no client effort. Saved-task runs are browser-surface, so no
        # sandbox model force needed here. demo=True strips the app identity.
        bot = "gemma"
        if model not in {m["value"] for m in OPERATOR_MODELS_DEMO}:
            model = OPERATOR_MODELS_DEMO[0]["value"]
        r = operator_agent.runner.start(bot, task_prompt, model=model,
                                        effort="", demo=True)
    else:
        r = operator_agent.runner.start(bot, task_prompt, model=model, effort=effort)
    if r.get("ok"):
        operator_tasks_store.mark_run(slug)
        return r, 200
    return r, 409


@bp.route("/operator/tasks/<slug>", methods=["DELETE"])
def operator_task_delete(slug):
    """Remove a saved task."""
    guard = _demo_tasks_guard()
    if guard:
        return guard
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


@bp.route("/operator/agent/say", methods=["POST"])
def operator_agent_say():
    """Mid-run steering (1.0.12): queue a user message for the LIVE run —
    delivered mid-loop by the steer hook (claude runtime) or as one more
    resumed turn at the exit seam. 409 when nothing is running (the client
    falls back to a normal dispatch). Allowed in demo: the demo runner is a
    separate process AND its steer queue is a separate file (demo-scoped
    OPERATOR_STEER_PATH + the .demo default backstop — a shared queue would
    let a visitor steer a live production run; found in review 2026-07-11),
    and a visitor already controls the task text — no new surface."""
    data = request.get_json(silent=True) or request.form
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="empty message"), 400
    if len(text) > 4000:
        return jsonify(ok=False, error="message too long (max 4000 chars)"), 413
    r = operator_agent.runner.steer(text)
    return (jsonify(r), 200) if r.get("ok") else (jsonify(r), 409)


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
    # in demo mode the agent has no the app transcript to tail (and we must not read
    # any the app bot's transcript) -> the live trace comes from the agent runner only.
    if bot and not DEMO:
        reasoning = _tail_reasoning(bot, since)
    return jsonify(driver=drv, events=_recent_events(30), reasoning=reasoning)


# ── Stage 2: reasoning relay  ──────────────────────────────
# Tail the driving bot's live session transcript JSONL → surface its assistant
# text (its reasoning/replies) so the operator chat shows thinking, not just
# clicks. Per-bot transcript dir = <config_dir>/projects/<cwd-slug>/; we take the
# most-recently-modified .jsonl there (the live session).
import glob as _glob

# bot → (config_dir, cwd) used to locate its transcript project dir.
_BOT_PROJECT = {
    "claude-a":     ("~/.claude",            "~/agents/claude-a"),
    "claude-a":  ("~/.claude",            "~/agents/claude-a"),
    "claude-a": ("~/.claude",            "~/agents/claude-a"),
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
    "claude-a": "/claude-agents/claude-a",
    "claude-a": "/claude-agents/claude-a",
    "claude-a": "/claude-agents/claude-a",
}
_BOT_LIVE_ENV = {"claude-b": ".config/claude-b"}


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
    # reliably (one MCP slot, a broker), so we don't mark it live for driving.
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
# codex/gpt models (default gpt-5.6-sol low per the owner). The 5.6 family ships three
# capability tiers (Sol flagship / Terra balanced / Luna fast) — each a distinct
# -m id with its OWN effort ladder (see EFFORT_BY_MODEL in operator.html):
# Sol adds max+ultra, Luna caps at minimal/low. Effort is the separate picker.
OPERATOR_MODELS_GPT = [
    {"value": "gpt-5.6-sol", "label": "GPT-5.6 Sol"},
    {"value": "gpt-5.6-terra", "label": "GPT-5.6 Terra"},
    {"value": "gpt-5.6-luna", "label": "GPT-5.6 Luna"},
    {"value": "gpt-5.5", "label": "GPT-5.5"},
]
# gemma drives via agy (Antigravity) — exposes the full agy model lineup on the owner
# flat Google sub. Gemini families use the effort picker for tier; the Claude/GPT-OSS
# ones have a fixed tier baked in (no effort). start() folds family+effort into the
# agy --model display string, e.g. "Gemini 3.5 Flash (High)".
OPERATOR_MODELS_GEMMA = [
    {"value": "Gemini 3.5 Flash", "label": "3.5 Flash"},
    {"value": "Claude Sonnet 4.6 (Thinking)", "label": "Sonnet 4.6"},
    {"value": "Claude Opus 4.6 (Thinking)", "label": "Opus 4.6"},
    {"value": "GPT-OSS 120B (Medium)", "label": "GPT-OSS 120B"},
]


# public demo: LOCKED 2-model choice on the gemma/agy runtime :
# Flash 3.5 Low default (first = picker default + server fallback), Sonnet 4.6
# as the heavier alt. Tier is baked into each value — the effort control is
# hidden in the demo UI, the lock owns effort.
OPERATOR_MODELS_DEMO = [
    {"value": "Gemini 3.5 Flash (Low)", "label": "3.5 Flash"},
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
