"""emu_input.py — button input for the EmulatorJS game harness (Pokémon
Emerald et al.) in the operator sandbox.

WHY THIS EXISTS: EmulatorJS binds keydown on the DOM, so the X11 keys the
desktop `computer` tool injects do NOT reach the emulator (verified 2026-07-21:
X11 Enter did nothing on the Emerald title). The game is driven through the
emulator's own JS API, window.EJS_emulator.gameManager.simulateInput(player,
button, value), reached over CDP against the sandbox chromium.

A GBA game is button-driven, not click-addressable — so this is a distinct
tool from click_target, exposed only on the desktop-sandbox surface.

Connection: chromium in the sandbox binds --remote-debugging-port on loopback;
sandbox_container's in-container bridge re-exposes it on the container IP
(sandbox_cdp_url()). We connect_over_cdp there and find the tab whose page has
window.EJS_emulator. A dedicated event-loop thread owns the attach (same shape
as BrowserSurface) so a wedged page fails one call, never the controller.
"""
from __future__ import annotations

import asyncio
import threading

# EmulatorJS GBA button ids (dumped live from EJS_emulator.controls[0]).
BUTTONS = {
    "a": 0, "b": 8, "select": 2, "start": 3,
    "up": 4, "down": 5, "left": 6, "right": 7,
    "l": 10, "r": 11,
}


class EmuInputError(RuntimeError):
    """The emulator page couldn't be reached or driven."""


class EmuInput:
    """CDP driver for the sandbox EmulatorJS page. Lazily attaches on first
    press; reconnects transparently if the page navigates or the tab changes."""

    def __init__(self, cdp_url: str) -> None:
        if not cdp_url:
            raise EmuInputError("no sandbox CDP url (container/bridge down?)")
        self._cdp_url = cdp_url
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        daemon=True, name="emu-input")
        self._thread.start()
        self._pw = None
        self._browser = None

    def _run(self, coro, timeout: float = 8.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except EmuInputError:
            raise
        except Exception as e:  # noqa: BLE001
            fut.cancel()
            raise EmuInputError(f"emu op failed: {e}") from e

    async def _browser_h(self):
        # connect_over_cdp handles can go stale; re-probe contexts each call and
        # reconnect if the handle is dead.
        if self._browser is not None:
            try:
                _ = self._browser.contexts
                return self._browser
            except Exception:  # noqa: BLE001
                self._browser = None
        if self._pw is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self._cdp_url)
        return self._browser

    async def _emu_page(self):
        """The tab whose window has EJS_emulator (the running game)."""
        b = await self._browser_h()
        for ctx in b.contexts:
            for pg in ctx.pages:
                if pg.is_closed():
                    continue
                try:
                    has = await asyncio.wait_for(
                        pg.evaluate("!!(window.EJS_emulator && "
                                    "window.EJS_emulator.gameManager)"),
                        timeout=1.5)
                    if has:
                        return pg
                except Exception:  # noqa: BLE001 — privileged/blank tab
                    continue
        raise EmuInputError("no running EmulatorJS game found in the sandbox "
                            "(is the game tab open and started?)")

    async def _press_async(self, button: str, hold_ms: int) -> dict:
        bid = BUTTONS.get(button.lower())
        if bid is None:
            raise EmuInputError(f"unknown button {button!r} "
                                f"(valid: {sorted(BUTTONS)})")
        pg = await self._emu_page()
        hold = max(16, min(int(hold_ms), 4000))
        await pg.evaluate(
            """async ({bid, hold}) => {
                const gm = window.EJS_emulator.gameManager;
                gm.simulateInput(0, bid, 1);
                await new Promise(r => setTimeout(r, hold));
                gm.simulateInput(0, bid, 0);
            }""", {"bid": bid, "hold": hold})
        return {"ok": True, "button": button, "hold_ms": hold}

    def press(self, button: str, hold_ms: int = 90) -> dict:
        """Tap one GBA button (down, hold hold_ms, up). Synchronous."""
        return self._run(self._press_async(button, hold_ms))

    def press_seq(self, buttons: list, hold_ms: int = 90,
                  gap_ms: int = 120) -> dict:
        """Tap a sequence of buttons with a gap between each."""
        done = []
        for btn in buttons:
            self.press(btn, hold_ms)
            done.append(btn)
            self._run(_sleep(gap_ms / 1000.0))
        return {"ok": True, "pressed": done}


async def _sleep(secs: float) -> None:
    await asyncio.sleep(secs)
