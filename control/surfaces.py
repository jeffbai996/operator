"""surfaces.py — one capture+inject interface over the three operator surfaces.

    browser          the operator's logged-in Chrome, over raw CDP
    desktop-sandbox  the isolated Xvfb display (computer-use display/actions)
    desktop-real     the live Windows desktop (computer-use win_backend) — INVASIVE

The macro controller and the MCP tools speak only this interface, so control
mode (direct vs macro) and surface (what screen) stay independent axes.

Browser injection is raw CDP `Input.dispatchMouseEvent`/`insertText` on an
async playwright attach running in a dedicated event-loop thread, every op
bounded by a timeout — NEVER page.mouse/page.evaluate unbounded (high-level
Playwright calls can block forever on a desynced connect_over_cdp
handle).

Safety: every inject first checks the shared STOP file. The cockpit STOP button
arms it; any armed stop newer than the surface's start kills all further
injection with SurfaceStopped — the hard floor under desktop-real autonomy.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import threading
import time

import numpy as np
from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
_CU_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "computer-use"))

SURFACES = ("auto", "browser", "desktop-sandbox", "desktop-real")
STOP_FILE = os.path.expanduser("~/.cache/computer-use/operator-stop.json")


@contextlib.contextmanager
def gui_lock(surface_name: str, timeout: float = 15.0):
    """File-based GUI lock context manager using fcntl.flock on
    ~/.cache/computer-use/gui-<surface_name>.lock so concurrent runs don't
    interleave X11 mouse/keyboard actions during a single computer action."""
    safe_name = "".join(c for c in (surface_name or "default") if c.isalnum() or c in "._-")
    lock_dir = Path(os.environ.get("COMPUTER_USE_OUTPUT_DIR") or Path.home() / ".cache/computer-use")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f"gui-{safe_name}.lock"

    deadline = time.time() + max(0.5, float(timeout or 15.0))
    f = open(lock_file, "w")
    try:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.time() >= deadline:
                    raise SurfaceError(f"timed out waiting for GUI lock on {surface_name}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            f.close()
        except OSError:
            pass


def stop_file() -> str:
    """This run's kill switch; legacy callers retain the original shared path."""
    return os.environ.get("OPERATOR_STOP_PATH") or STOP_FILE

CDP_URL = os.environ.get("OPERATOR_CDP") or "http://127.0.0.1:9222"

# Color scheme forced onto every CDP-attached page (see _force_dark_media).
# The host browser/OS runs dark; attaching flips pages to light, so we pin it.
_MEDIA_SCHEME = os.environ.get("OPERATOR_COLOR_SCHEME") or "dark"


class SurfaceError(RuntimeError):
    """A capture/inject op failed or the surface is unavailable."""


class SurfaceStopped(SurfaceError):
    """The user hit STOP — all injection halts immediately."""


def arm_stop() -> None:
    """Arm the kill switch (the cockpit STOP button calls this via the view)."""
    path = stop_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time()}, f)


def _stop_armed_since(ts: float) -> bool:
    try:
        with open(stop_file(), encoding="utf-8") as f:
            return float(json.load(f).get("ts", 0)) > ts
    except (OSError, ValueError):
        return False


def _load_cu_module(fname: str):
    """Import a computer-use module by file path (its dir name has a dash, so it
    can't be a normal package import)."""
    path = os.path.join(_CU_DIR, fname)
    if not os.path.exists(path):
        raise SurfaceError(f"computer-use module missing: {path}")
    name = "cu_" + fname[:-3]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _BaseSurface:
    name = "base"

    def __init__(self) -> None:
        self._armed_ts = time.time()

    def _check_stop(self) -> None:
        if _stop_armed_since(self._armed_ts):
            raise SurfaceStopped("STOP engaged — injection halted")

    # capture
    # INVARIANT (§3.2c): frame() is always a FRESH on-demand capture (CDP
    # captureScreenshot / a new screenshot file). It must never be satisfied
    # from the cockpit's throttled MJPEG feed — the agent reasons over what
    # frame() returns, and a stale feed frame is how "can't find X when it's
    # right there" happens. All implementations comply.
    def frame(self) -> np.ndarray:  # RGB (H, W, 3)
        raise NotImplementedError

    def size(self) -> tuple:
        h, w = self.frame().shape[:2]
        return (w, h)

    # inject (all implementations call _check_stop() first)
    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        raise NotImplementedError

    def drag(self, x1: int, y1: int, x2: int, y2: int,
             duration_ms: int = 350) -> None:
        raise NotImplementedError

    def move(self, x: int, y: int) -> None:
        raise NotImplementedError

    def type_text(self, text: str) -> None:
        raise NotImplementedError

    def key(self, combo: str) -> None:
        raise NotImplementedError

    def scroll(self, x: int, y: int, direction: str = "down",
               amount: int = 3) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ── browser (raw CDP over an async playwright attach) ────────────────────────
class BrowserSurface(_BaseSurface):
    """Capture + inject on the active page of the operator's Chrome.

    A dedicated event-loop thread owns the playwright attach; sync callers
    submit coroutines with a hard timeout, so a wedged page can only fail the
    one op — it can never hang the controller."""
    name = "browser"

    _OP_TIMEOUT = 6.0

    def __init__(self, cdp_url: str = CDP_URL) -> None:
        super().__init__()
        self._cdp_url = cdp_url
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        daemon=True, name="browser-surface")
        self._thread.start()
        self._pw = None
        self._browser = None
        self._page = None
        self._sess = None
        self._run(self._attach(), timeout=20)

    def _run(self, coro, timeout: float = _OP_TIMEOUT):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except Exception as e:
            fut.cancel()
            if isinstance(e, (SurfaceError, SurfaceStopped)):
                raise
            raise SurfaceError(f"browser op failed: {e}") from e

    async def _attach(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(
            self._cdp_url, **({'no_defaults': True} if os.environ.get('OPERATOR_RUN_ID') else {}))
        await self._pick_page()

    async def _pick_page(self) -> None:
        """Bind to the conversation's owned target; unscoped local control follows visibility."""
        if not self._browser.contexts:
            raise SurfaceError("no browser context on the CDP endpoint")
        cid = os.environ.get("OPERATOR_CONVERSATION_ID", "")
        if cid:
            from operator_browser_target import resolve_owned_page
            if not hasattr(self, "_target_ids"):
                self._target_ids = {}
            try:
                chosen = await resolve_owned_page(self._browser, cid, self._page, self._target_ids)
            except RuntimeError as e:
                raise SurfaceError(str(e)) from e
            if chosen is not self._page:
                self._page, self._sess = chosen, None
                await self._force_dark_media()
            return
        pages = [p for p in self._browser.contexts[0].pages if not p.is_closed()]
        if not pages:
            raise SurfaceError("no open pages")
        chosen = pages[-1]
        if len(pages) > 1:
            for p in pages:
                try:
                    vis = await asyncio.wait_for(
                        p.evaluate("document.visibilityState"), timeout=0.8)
                    if vis == "visible":
                        chosen = p
                        break
                except Exception:
                    continue
        if chosen is not self._page:
            self._page = chosen
            self._sess = None
            await self._force_dark_media()

    async def _session(self):
        if self._sess is None:
            self._sess = await self._page.context.new_cdp_session(self._page)
        return self._sess

    async def _force_dark_media(self) -> None:
        """Re-assert dark mode on the bound page.

        Attaching over CDP makes every page report
        `prefers-color-scheme: light` even though the real Chrome (and the OS)
        is dark — sites then render their light theme for the whole run.

        The browser is NOT at fault: a raw CDP
        `Runtime.evaluate` on this same browser reports dark=True BEFORE a
        Playwright attach, False DURING it, and dark=True again after detach.
        Playwright pushes its own `Emulation.setEmulatedMedia` defaults on
        attach.

        Clearing the override (`features: []`) looks like the tidier fix and
        does sometimes restore dark, but it was measured to be UNRELIABLE —
        on an attached tab it frequently leaves the page reporting light, both
        before and after navigation. Explicitly forcing `dark` is the only
        variant that tested deterministic, so that is what we send. The cost
        is that this pins dark rather than following the OS; if the host ever
        moves to light mode, change the value here (`_MEDIA_SCHEME`).

        The override is per-target, so this re-runs on every page bind: it
        survives navigation within a tab, but a newly-bound tab starts
        overridden again. Best-effort — a page that can't take the command
        (privileged/blank target) is not worth failing an operator run over.
        """
        try:
            sess = await self._session()
            await asyncio.wait_for(
                sess.send("Emulation.setEmulatedMedia",
                          {"features": [{"name": "prefers-color-scheme",
                                         "value": _MEDIA_SCHEME}]}),
                timeout=2.0)
        except Exception:  # noqa: BLE001 — cosmetic; never break capture/inject
            self._sess = None

    async def _send(self, method: str, params: dict | None = None,
                    timeout: float = 4.0):
        cid = os.environ.get("OPERATOR_CONVERSATION_ID", "")
        if cid:
            from operator_browser_target import owned_targets
            if (self._page is None or self._page.is_closed()
                    or self._target_ids.get(self._page) not in owned_targets(cid)):
                raise SurfaceError("owned browser target changed; capture a fresh frame")
        sess = await self._session()
        try:
            return await asyncio.wait_for(sess.send(method, params or {}),
                                          timeout=timeout)
        except Exception:
            self._sess = None    # stale session (page nav) — rebuild next call
            raise

    def frame(self) -> np.ndarray:
        async def _grab():
            await self._pick_page()
            res = await self._send("Page.captureScreenshot",
                                   {"format": "jpeg", "quality": 90}, timeout=4.0)
            import base64
            import io
            img = Image.open(io.BytesIO(base64.b64decode(res["data"])))
            arr = np.asarray(img.convert("RGB"))
            # CDP screenshots come back in DEVICE pixels; input events take CSS
            # pixels. Record the frame→CSS scale so every inject converts —
            # otherwise clicks land down-right of the perceived target on any
            # DPR>1 window (the classic vision-click desync).
            scale_key = (id(self._page), arr.shape[1], arr.shape[0])
            if getattr(self, "_scale_key", None) != scale_key:
                try:
                    m = await self._send("Page.getLayoutMetrics", timeout=3.0)
                    vp = m.get("cssLayoutViewport") or m.get("layoutViewport") or {}
                    cw, ch = vp.get("clientWidth"), vp.get("clientHeight")
                    if cw and ch:
                        self._scale_x = arr.shape[1] / float(cw)
                        self._scale_y = arr.shape[0] / float(ch)
                        self._scale_key = scale_key
                except Exception:
                    pass
            return arr
        return self._run(_grab())

    def _to_css(self, x, y) -> tuple:
        """Frame-pixel coords (what perception saw) → CSS-pixel input coords."""
        sx = getattr(self, "_scale_x", 1.0) or 1.0
        sy = getattr(self, "_scale_y", 1.0) or 1.0
        return (float(x) / sx, float(y) / sy)

    def attach_overwatch_session(self, overwatch_session, quality: int = 75) -> bool:
        """Start continuous CDP screencast + Network/Runtime/Log sidecar on the bound page."""
        async def _start():
            await self._pick_page()
            sess = await self._session()
            url = getattr(self._page, "url", "/") or "/"
            return await overwatch_session.start(sess, url=url, quality=quality)

        try:
            return bool(self._run(_start(), timeout=4.0))
        except Exception:
            return False

    def detach_overwatch_session(self, overwatch_session) -> None:
        """Stop continuous CDP screencast and flush partial slice on teardown."""
        async def _stop():
            await overwatch_session.stop()

        try:
            self._run(_stop(), timeout=2.5)
        except Exception:
            pass

    @staticmethod
    def _latest_snapshot_refs() -> list[tuple[str, str, str]]:
        """Parse (role, label, ref) tuples from the newest Playwright page-*.yml snapshot."""
        import glob
        out_dir = os.environ.get(
            "COMPUTER_USE_OUTPUT_DIR",
            os.path.expanduser("~/.cache/computer-use"),
        )
        try:
            cands = sorted(
                glob.glob(os.path.join(out_dir, "page-*.yml")),
                key=os.path.getmtime,
                reverse=True,
            )
            if not cands:
                return []
            raw = open(cands[0], encoding="utf-8", errors="replace").read(131072)
        except Exception:
            return []
        refs: list[tuple[str, str, str]] = []
        pat = re.compile(r'-\s+([a-zA-Z0-9_-]+)(?:\s+"([^"]*)")?[^\n]*?\[ref=((?:f\d+)?e\d+)\]')
        for m in pat.finditer(raw):
            refs.append((m.group(1).lower(), (m.group(2) or "").strip().lower(), m.group(3)))
        return refs

    def interactive_elements(self, limit: int = 40) -> list[dict]:
        """Query visible interactive DOM elements and return frame-pixel center boxes."""
        js = f"""(() => {{
            const sel = 'a[href], button, input, select, textarea, summary, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="checkbox"], [role="switch"], [onclick]';
            const nodes = Array.from(document.querySelectorAll(sel));
            const out = [];
            const vw = window.innerWidth || document.documentElement.clientWidth || 1280;
            const vh = window.innerHeight || document.documentElement.clientHeight || 800;
            for (const el of nodes) {{
                if (out.length >= {int(limit)}) break;
                const style = window.getComputedStyle(el);
                if (!style || style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity || '1') < 0.05) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 6 || r.height < 6) continue;
                if (r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) continue;
                const rawLabel = (
                    el.getAttribute('aria-label') ||
                    el.innerText ||
                    el.value ||
                    el.getAttribute('placeholder') ||
                    el.getAttribute('title') ||
                    el.getAttribute('name') ||
                    el.tagName.toLowerCase()
                );
                const tag = el.tagName.toUpperCase();
                const role = (el.getAttribute('role') || (
                    tag === 'A' ? 'link' :
                    tag === 'BUTTON' ? 'button' :
                    tag === 'SELECT' ? 'combobox' :
                    tag === 'TEXTAREA' ? 'textbox' :
                    tag === 'INPUT' ? (['button','submit','reset'].includes((el.type||'').toLowerCase()) ? 'button' : (['checkbox','radio'].includes((el.type||'').toLowerCase()) ? (el.type||'').toLowerCase() : 'textbox')) :
                    tag.toLowerCase()
                )).toLowerCase();
                const pwRef = (el._ariaRef && el._ariaRef.ref) || el.getAttribute('data-pw-ref') || '';
                const label = String(rawLabel || el.tagName.toLowerCase()).replace(/\\s+/g, ' ').trim().slice(0, 48);
                out.push({{
                    label: label || el.tagName.toLowerCase(),
                    role,
                    ref: String(pwRef || ''),
                    cx: r.left + r.width / 2,
                    cy: r.top + r.height / 2,
                    w: r.width,
                    h: r.height,
                }});
            }}
            return out;
        }})()"""

        async def _query():
            await self._pick_page()
            res = await self._send(
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
                timeout=2.5,
            )
            val = ((res or {}).get("result") or {}).get("value")
            return val if isinstance(val, list) else []

        try:
            raw_items = self._run(_query(), timeout=3.5)
        except Exception:
            return []
        snap_refs = self._latest_snapshot_refs()
        used_refs: set[str] = set()
        sx = getattr(self, "_scale_x", 1.0) or 1.0
        sy = getattr(self, "_scale_y", 1.0) or 1.0
        items: list[dict] = []
        for idx, it in enumerate(raw_items[:limit], start=1):
            if not isinstance(it, dict):
                continue
            lbl = str(it.get("label") or "element")
            lbl_low = lbl.lower()
            role_low = str(it.get("role") or "").lower()
            ref_val = str(it.get("ref") or "").strip()
            if not ref_val and snap_refs:
                for s_role, s_lbl, s_ref in snap_refs:
                    if s_ref in used_refs:
                        continue
                    if s_lbl and s_lbl == lbl_low and (not role_low or s_role == role_low):
                        ref_val = s_ref
                        break
                if not ref_val:
                    for s_role, s_lbl, s_ref in snap_refs:
                        if s_ref in used_refs:
                            continue
                        if s_lbl and (s_lbl == lbl_low or lbl_low in s_lbl or s_lbl in lbl_low):
                            ref_val = s_ref
                            break
            if ref_val:
                used_refs.add(ref_val)
            entry = {
                "id": idx,
                "label": lbl,
                "x": int(round(float(it.get("cx", 0.0)) * sx)),
                "y": int(round(float(it.get("cy", 0.0)) * sy)),
                "w": max(6, int(round(float(it.get("w", 16.0)) * sx))),
                "h": max(6, int(round(float(it.get("h", 16.0)) * sy))),
            }
            if ref_val:
                entry["ref"] = ref_val
            items.append(entry)
        return items

    def click(self, x, y, button="left", clicks=1) -> None:
        self._check_stop()
        x, y = self._to_css(x, y)

        async def _click():
            await self._send("Input.dispatchMouseEvent",
                             {"type": "mouseMoved", "x": float(x), "y": float(y)})
            await asyncio.sleep(0.02)
            for n in range(1, clicks + 1):
                await self._send("Input.dispatchMouseEvent",
                                 {"type": "mousePressed", "x": float(x), "y": float(y),
                                  "button": button, "clickCount": n})
                await asyncio.sleep(0.03)
                await self._send("Input.dispatchMouseEvent",
                                 {"type": "mouseReleased", "x": float(x), "y": float(y),
                                  "button": button, "clickCount": n})
        self._run(_click())

    def drag(self, x1, y1, x2, y2, duration_ms=350) -> None:
        self._check_stop()
        x1, y1 = self._to_css(x1, y1)
        x2, y2 = self._to_css(x2, y2)

        async def _drag():
            steps = max(4, int(duration_ms / 40))
            await self._send("Input.dispatchMouseEvent",
                             {"type": "mouseMoved", "x": float(x1), "y": float(y1)})
            await self._send("Input.dispatchMouseEvent",
                             {"type": "mousePressed", "x": float(x1), "y": float(y1),
                              "button": "left", "clickCount": 1})
            for i in range(1, steps + 1):
                ix = x1 + (x2 - x1) * i / steps
                iy = y1 + (y2 - y1) * i / steps
                await self._send("Input.dispatchMouseEvent",
                                 {"type": "mouseMoved", "x": float(ix), "y": float(iy),
                                  "button": "left"})
                await asyncio.sleep(duration_ms / 1000.0 / steps)
            await self._send("Input.dispatchMouseEvent",
                             {"type": "mouseReleased", "x": float(x2), "y": float(y2),
                              "button": "left", "clickCount": 1})
        self._run(_drag(), timeout=self._OP_TIMEOUT + duration_ms / 1000.0)

    def move(self, x, y) -> None:
        self._check_stop()
        x, y = self._to_css(x, y)
        self._run(self._send("Input.dispatchMouseEvent",
                             {"type": "mouseMoved", "x": float(x), "y": float(y)}))

    def type_text(self, text) -> None:
        self._check_stop()
        self._run(self._send("Input.insertText", {"text": str(text)}))

    def key(self, combo) -> None:
        """Key combo via Input.dispatchKeyEvent. Single keys and simple
        modifier+key combos (ctrl+a) — the macro layer needs no more."""
        self._check_stop()
        parts = [p.strip() for p in str(combo).split("+") if p.strip()]
        mods = 0
        _MOD_BITS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4,
                     "shift": 8}
        key = parts[-1] if parts else ""
        for p in parts[:-1]:
            mods |= _MOD_BITS.get(p.lower(), 0)
        _NAMED = {"enter": "Enter", "return": "Enter", "esc": "Escape",
                  "escape": "Escape", "tab": "Tab", "space": " ",
                  "backspace": "Backspace", "delete": "Delete",
                  "up": "ArrowUp", "down": "ArrowDown",
                  "left": "ArrowLeft", "right": "ArrowRight"}
        key = _NAMED.get(key.lower(), key)

        async def _key():
            await self._send("Input.dispatchKeyEvent",
                             {"type": "keyDown", "key": key, "modifiers": mods})
            await asyncio.sleep(0.02)
            await self._send("Input.dispatchKeyEvent",
                             {"type": "keyUp", "key": key, "modifiers": mods})
        self._run(_key())

    def scroll(self, x, y, direction="down", amount=3) -> None:
        self._check_stop()
        x, y = self._to_css(x, y)
        dy = {"down": 120, "up": -120}.get(direction, 120) * amount
        dx = {"right": 120, "left": -120}.get(direction, 0) * amount
        if direction in ("left", "right"):
            dy = 0
        self._run(self._send("Input.dispatchMouseEvent",
                             {"type": "mouseWheel", "x": float(x), "y": float(y),
                              "deltaX": float(dx), "deltaY": float(dy)}))

    def close(self) -> None:
        async def _close():
            try:
                if self._browser:
                    await self._browser.close()
            finally:
                if self._pw:
                    await self._pw.stop()
        try:
            self._run(_close(), timeout=5)
        except SurfaceError:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


# ── desktop (computer-use backends) ──────────────────────────────────────────
class _DesktopSurface(_BaseSurface):
    """Shared shape for the two computer-use backends: translate the surface
    interface into the backend's `computer_20250124`-style action dicts."""
    name = "desktop"

    def __init__(self) -> None:
        super().__init__()
        self._out_dir = os.environ.get(
            "COMPUTER_USE_OUTPUT_DIR",
            os.path.expanduser("~/.cache/computer-use"))

    # subclasses set: self._exec(action: dict), self._shot() -> path
    def frame(self) -> np.ndarray:
        path = self._shot()
        return np.asarray(Image.open(path).convert("RGB"))

    def _do(self, action: dict) -> None:
        self._check_stop()
        self._exec(action)

    def click(self, x, y, button="left", clicks=1) -> None:
        kind = {("left", 1): "left_click", ("left", 2): "double_click",
                ("left", 3): "triple_click", ("right", 1): "right_click",
                ("middle", 1): "middle_click"}.get((button, clicks), "left_click")
        self._do({"action": kind, "coordinate": [int(x), int(y)]})

    def drag(self, x1, y1, x2, y2, duration_ms=350) -> None:
        # xdotool/SendKeys path has no smooth drag; press-move-release
        self._do({"action": "mouse_move", "coordinate": [int(x1), int(y1)]})
        self._do({"action": "left_mouse_down"})
        time.sleep(min(duration_ms, 1000) / 1000.0 / 2)
        self._do({"action": "mouse_move", "coordinate": [int(x2), int(y2)]})
        time.sleep(min(duration_ms, 1000) / 1000.0 / 2)
        self._do({"action": "left_mouse_up"})

    def move(self, x, y) -> None:
        self._do({"action": "mouse_move", "coordinate": [int(x), int(y)]})

    def type_text(self, text) -> None:
        self._do({"action": "type", "text": str(text)})

    def key(self, combo) -> None:
        self._do({"action": "key", "text": str(combo)})

    def scroll(self, x, y, direction="down", amount=3) -> None:
        self._do({"action": "scroll", "coordinate": [int(x), int(y)],
                  "scroll_direction": direction, "scroll_amount": int(amount)})


class SandboxSurface(_DesktopSurface):
    """A REAL isolated Linux desktop, running in a Docker container (own rootfs,
    network/PID namespace, non-root user). Driven via `docker exec` scrot/xdotool
    — nothing it does can touch the host. The container is persistent: it survives
    leaving Operator and is only destroyed by an explicit delete."""
    name = "desktop-sandbox"

    def __init__(self) -> None:
        super().__init__()
        self._sb = _load_cu_module("sandbox_container.py")
        self._sb.ensure()

    def _exec(self, action: dict) -> None:
        self._sb.execute(action)

    def _shot(self) -> str:
        return self._sb.screenshot(self._out_dir)

    def size(self) -> tuple:
        return self._sb.size()


class RealDesktopSurface(_DesktopSurface):
    """The live Windows desktop — INVASIVE (moves the real cursor).

    Constructing it requires the explicit opt-in env OPERATOR_REAL_OK=1, which
    only the cockpit's per-session confirm flow sets. Never a default."""
    name = "desktop-real"

    def __init__(self, real_ok: bool = False) -> None:
        if not real_ok and os.environ.get("OPERATOR_REAL_OK") != "1":
            raise SurfaceError(
                "desktop-real needs explicit confirmation (OPERATOR_REAL_OK=1) — "
                "it drives the REAL desktop")
        super().__init__()
        self._win = _load_cu_module("win_backend.py")
        self._win.ensure()

    def _exec(self, action: dict) -> None:
        self._win.execute(action, "windows-primary")

    def _shot(self) -> str:
        return self._win.screenshot("windows-primary", self._out_dir)

    def size(self) -> tuple:
        return self._win.screen_size()


def get_surface(name: str):
    """Factory. Raises SurfaceError for unknown names or a blocked desktop-real."""
    if name in ("", None, "browser"):
        return BrowserSurface()
    if name == "auto":
        if (os.environ.get("OPERATOR_REAL_OK") == "1"
                and os.environ.get("OPERATOR_ACTIVE_SURFACE") == "desktop-real"):
            return RealDesktopSurface(real_ok=True)
        return SandboxSurface()
    if name == "desktop-sandbox":
        return SandboxSurface()
    if name == "desktop-real":
        return RealDesktopSurface()
    raise SurfaceError(f"unknown surface {name!r} (valid: {', '.join(SURFACES)})")
