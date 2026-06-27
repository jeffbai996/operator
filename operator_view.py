"""Browser operator — live view + full remote control of the logged-in Chrome.

One self-contained surface (full-screen on an iPad behind a reverse proxy) that shows the
real Chrome the squad's computer-use drives and lets you take the wheel live —
click, type, navigate — interleaving freely with whatever a bot is doing in the
same browser (shared mouse; last action wins). "See it, steer it." (Jeff
2026-06-25; refined for click/keyboard control + more controls 2026-06-26.)

Zero new deps — playwright + aiohttp are already in the host app venv:
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

from flask import Blueprint, Response, jsonify, render_template, request
import operator_agent  # the headless-claude agent runner (option 1)

CDP_URL = "http://127.0.0.1:9222"
FRAME_INTERVAL = 0.1       # ~10fps; eager CDP-session rebind keeps it stable, dedup keeps a static page ~0.5fps
JPEG_QUALITY = 60
IDLE_STOP_AFTER = 90.0

bp = Blueprint("operator", __name__,
                template_folder="templates", static_folder="static")


import base64 as _b64ph
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
    _frame_hash: int = 0          # hash of the last frame — skip re-publishing identical frames (battery/data)
    _cdp_page: object = None      # the page the cached CDP session is bound to (recreate on change)
    last_view: float = 0.0
    status: str = "idle"          # idle | connecting | live | error
    detail: str = ""
    vw: int = 0                   # live viewport size (for click coord scaling)
    vh: int = 0
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
        try:
            self._loop.run_until_complete(self._grab_loop())
        except Exception as e:  # noqa: BLE001
            self.status, self.detail = "error", str(e)
        finally:
            self._running = False
            if self.status == "live":
                self.status = "idle"

    def _hard_relaunch_chrome(self) -> None:
        """Kill any (possibly wedged) Chrome + relaunch the logged-in one. Used when
        the browser is alive but its CDP page ops hang (screenshots time out)."""
        import os, subprocess
        try:
            subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                            "Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
        attach = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browse", "chrome-attach.sh")
        if os.path.exists(attach):
            try:
                subprocess.Popen(["bash", attach], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:  # noqa: BLE001
                pass
        import urllib.request
        for _ in range(20):
            time.sleep(1.0)
            try:
                urllib.request.urlopen(CDP_URL + "/json/version", timeout=2).read()
                return
            except Exception:  # noqa: BLE001
                continue

    def _ensure_chrome_alive(self) -> None:
        """If CDP is unreachable (Chrome died), relaunch the logged-in Chrome via
        chrome-attach.sh — the same thing the desktop 'Open Bot Chrome' script does.
        Blocking + best-effort; runs in the streamer thread before an attach."""
        import os, subprocess, urllib.request, json as _json
        alive = False
        try:
            # /json (target list) needs the browser to actually service a request,
            # not just answer /json/version (a wedged Chrome still answers version).
            raw = urllib.request.urlopen(CDP_URL + "/json", timeout=3).read()
            _json.loads(raw)
            alive = True
        except Exception:  # noqa: BLE001 — dead OR wedged → (re)launch
            alive = False
        if alive:
            return
        attach = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browse", "chrome-attach.sh")
        if not os.path.exists(attach):
            return
        # if a wedged Chrome process is lingering, kill it first so the relaunch takes.
        try:
            subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                            "Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
            time.sleep(1.5)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.detail = "relaunching Chrome…"
            subprocess.Popen(["bash", attach], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            return
        # wait (up to ~20s) for CDP to come up
        for _ in range(20):
            time.sleep(1.0)
            try:
                urllib.request.urlopen(CDP_URL + "/json/version", timeout=2).read()
                return
            except Exception:  # noqa: BLE001
                continue

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
        # also install on the CURRENTLY-open page (init script only covers future loads)
        try:
            await self._page.evaluate("""
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
                async with self._iolock():
                    png = await self._grab(self._page)
                if png:
                    # frame-dedup: only publish (bump frame_ts) when the image actually
                    # changed, so a STATIC page streams ~nothing instead of full fps.
                    h = hash(png)
                    if h != self._frame_hash:
                        self._frame_hash = h
                        self.frame = png
                        self.frame_ts = time.monotonic()
                    _misses = 0
                else:
                    _misses += 1
                    if _misses >= 4:
                        # wedged Chrome (alive but screenshots hang/fail) → hard
                        # relaunch; soft reattach can't fix a hung-but-alive browser.
                        _misses = 0
                        self.status = "connecting"; self.detail = "Chrome wedged — relaunching"
                        try: await self._teardown()
                        except Exception: pass
                        self._hard_relaunch_chrome()
                        await self._attach()
                if not self.vw:
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
            # the CDP session is bound to a page — if the active page changed (nav /
            # tab switch), the old session is stale and would time out. Recreate it
            # eagerly for the current page instead of capturing through a dead one.
            if sess is None or getattr(self, "_cdp_page", None) is not page:
                try:
                    if sess is not None:
                        try: await sess.detach()
                        except Exception: pass
                except Exception: pass
                sess = await page.context.new_cdp_session(page)
                self._cdp = sess
                self._cdp_page = page
            res = await asyncio.wait_for(
                sess.send("Page.captureScreenshot", {"format": "jpeg", "quality": JPEG_QUALITY}),
                timeout=4.0)
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
            self._cdp = None; self._cdp_page = None  # rebuild next grab
            try:
                return await asyncio.wait_for(
                    page.screenshot(type="jpeg", quality=JPEG_QUALITY, animations="disabled"),
                    timeout=4.0)
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
                    try:
                        await closing.goto("chrome://newtab/",
                                           wait_until="domcontentloaded", timeout=15000)
                    except Exception:  # noqa: BLE001
                        pass
                    self._page = closing; self._cdp = None; self._update_viewport()
                    return {"ok": True, "reset": True}
                await closing.close()
                live = [p for p in ctx.pages if not p.is_closed()]
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
            await pg.goto("chrome://newtab/", wait_until="domcontentloaded", timeout=15000)
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
                # human-ish: glide the cursor in (steps) + a short settle, so it
                # isn't a zero-movement instant click (a bot-detection tell).
                await p.mouse.move(x, y, steps=12)
                await asyncio.sleep(0.05)
                if kind == "dblclick_at":
                    await p.mouse.dblclick(x, y)
                else:
                    await p.mouse.down(); await asyncio.sleep(0.04); await p.mouse.up()
                return {"ok": True, "url": p.url, "title": await p.title(),
                        "px": [round(x), round(y)]}
            elif kind == "type":
                await p.keyboard.type(val, delay=35)
            elif kind == "key":
                await p.keyboard.press(val or "Enter")
            elif kind == "scroll":
                # numeric dy/dx → precise user wheel/touch scroll; else keyword amounts.
                dx = action.get("dx"); dy = action.get("dy")
                if isinstance(dy, (int, float)) or isinstance(dx, (int, float)):
                    fdx, fdy = float(dx or 0), float(dy or 0)
                    # mouse.wheel dispatches at the CURRENT mouse pos — if that's the
                    # (0,0) corner (no prior move) Chrome eats up-scroll (you're already
                    # at the corner's top). Park the mouse over the page center first so
                    # the wheel lands on real content and BOTH directions work.
                    try:
                        dims = await self._viewport_dims(p)
                        await p.mouse.move(dims["w"] / 2, dims["h"] / 2)
                    except Exception:
                        pass
                    await p.mouse.wheel(fdx, fdy)
                    # belt-and-suspenders: also nudge via JS (no-op on eval-blocked sites)
                    try:
                        await p.evaluate("(d)=>window.scrollBy(d.x,d.y)", {"x": fdx, "y": fdy})
                    except Exception:
                        pass
                else:
                    amt = {"up": -600, "down": 600, "top": -100000,
                           "bottom": 100000}.get(val, 600)
                    try:
                        dims = await self._viewport_dims(p)
                        await p.mouse.move(dims["w"] / 2, dims["h"] / 2)
                    except Exception:
                        pass
                    await p.mouse.wheel(0, amt)
            elif kind == "back":
                await p.go_back(timeout=15000)
            elif kind == "forward":
                await p.go_forward(timeout=15000)
            elif kind == "reload":
                await p.reload(timeout=20000)
            elif kind in ("mousedown_at", "mouseup_at"):  # press-and-hold (captchas)
                dims = await self._viewport_dims(p)
                x = float(action.get("x", 0)) * dims["w"]
                y = float(action.get("y", 0)) * dims["h"]
                if kind == "mousedown_at":
                    await p.mouse.move(x, y, steps=6)
                    await p.mouse.down()
                else:
                    await p.mouse.up()
                return {"ok": True, "url": p.url, "title": await p.title(), "px": [round(x), round(y)]}
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
                return {"ok": True, "url": p.url, "title": await p.title(),
                        "px": [round(x1), round(y1)]}
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
                await p.goto("chrome://newtab/", wait_until="domcontentloaded", timeout=20000)
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
            return {"ok": True, "url": p.url, "title": await p.title()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        finally:
            try: _lk.release()
            except Exception: pass


_streamer = _Streamer()


# ── routes ────────────────────────────────────────────────────────────────
@bp.route("/operator")
def operator_page():
    from flask import make_response
    resp = make_response(render_template("operator.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


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
            elif f and (now - last_push) > 2.0:
                last_push = now                       # ~0.5fps heartbeat of last frame (static page)
                yield _part(f)
            elif not f and (now - last_push) > 2.0:
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


@bp.route("/operator/status")
def operator_status():
    _streamer.last_view = time.monotonic()
    fresh = (_streamer.frame is not None
             and (time.monotonic() - _streamer.frame_ts) < 6.5)
    cur_url = ""
    try:
        if _streamer._page is not None:
            cur_url = _streamer._page.url or ""
    except Exception:
        cur_url = ""
    lx, ly, lt = _streamer.last_click
    click = None
    if lt and (time.monotonic() - lt) < 1.2:
        click = {"x": round(lx, 4), "y": round(ly, 4), "age": round(time.monotonic() - lt, 3)}
    return jsonify(status=_streamer.status, detail=_streamer.detail,
                   has_frame=fresh, vw=_streamer.vw, vh=_streamer.vh, url=cur_url,
                   click=click)


@bp.route("/operator/steer", methods=["POST"])
def operator_steer():
    data = request.get_json(silent=True) or request.form
    action = {"kind": data.get("kind"), "value": data.get("value", ""),
              "x": data.get("x", 0), "y": data.get("y", 0)}
    if not action["kind"]:
        return jsonify(ok=False, error="missing action kind"), 400
    return jsonify(_streamer.run_action(action))


# ── Live-session driving (Jeff 2026-06-26) ──────────────────────────────────
# Dispatch a task to one of the host bots' real Discord sessions; the bot
# runs it on the SAME shared Chrome the operator views. The browser actions are
# surfaced via the MCP action-tap (operator-events.ndjson) which every bot's
# playwright-mcp wrapper writes to — so the operator shows "🤖 <bot> · Clicking…"
# + the step trail regardless of which bot is driving. (Reasoning relay = stage 2.)
import json as _json
import os as _os

# The 5 drivers: host bots that can take the wheel. home_channel = where the
# operator posts the task (the running bot picks it up as a prompt). `key` is the
# bot name the action-tap stamps events with (must match detect_bot()).
DRIVERS = [
    {"key": "claude-a", "label": "claude-a"},
    {"key": "claude-b", "label": "claude-b"},
    {"key": "gpt", "label": "gpt"},
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
        return {"bot": last.get("bot"), "action": last.get("action"),
                "detail": last.get("detail", "")}
    return None


@bp.route("/operator/drivers")
def operator_drivers():
    """The pickable drivers (claude-a / claude-b) — the operator runs them headless."""
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
    r = operator_agent.runner.start(bot, task, model=model, effort=effort)
    return (jsonify(r), 200) if r.get("ok") else (jsonify(r), 409)


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
    if bot:
        reasoning = _tail_reasoning(bot, since)
    return jsonify(driver=drv, events=_recent_events(30), reasoning=reasoning)


# ── Stage 2: reasoning relay (Jeff 2026-06-26) ──────────────────────────────
# Tail the driving bot's live session transcript JSONL → surface its assistant
# text (its reasoning/replies) so the operator chat shows thinking, not just
# clicks. Per-bot transcript dir = <config_dir>/projects/<cwd-slug>/; we take the
# most-recently-modified .jsonl there (the live session).
import glob as _glob

# bot → (config_dir, cwd) used to locate its transcript project dir.
_BOT_PROJECT = {
    "claude-a":     ("~/.claude",            "~/agents/claude-a"),
    "claude-a":  ("~/.claude",            "~/agents/claude-a"),
    "claude-b": ("~/.claude",            "~/agents/claude-b"),
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
    "claude-a": "/agents/claude-a",
    "claude-b": "/agents/claude-b",
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
    # reliably (one MCP slot, IBKR), so we don't mark it live for driving.
    return live


# Model picker options. The VALUE is the alias (opus/sonnet/haiku) — claude
# resolves an alias to the *latest* of that family, so the actual model the agent
# runs is always current. The LABEL is the human version; bump these two lines
# when a family's latest version changes (the only manual touch-point).
OPERATOR_MODELS = [
    {"value": "opus", "label": "Opus 4.8"},
    {"value": "sonnet", "label": "Sonnet 4.6"},
    {"value": "haiku", "label": "Haiku 4.5"},
]
# codex/gpt models (default gpt-5.5 medium per Jeff).
OPERATOR_MODELS_GPT = [
    {"value": "gpt-5.5", "label": "GPT-5.5"},
    {"value": "gpt-5.4", "label": "GPT-5.4"},
]


@bp.route("/operator/models")
def operator_models():
    driver = request.args.get("driver", "")
    if driver == "gpt":
        return jsonify(models=OPERATOR_MODELS_GPT)
    return jsonify(models=OPERATOR_MODELS)
