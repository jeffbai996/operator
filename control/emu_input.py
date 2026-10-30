"""Bounded EmulatorJS input and checkpoints on the selected sandbox browser."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

# Libretro RetroPad IDs, also used by EmulatorJS simulateInput.
BUTTONS = {"a": 8, "b": 0, "select": 2, "start": 3,
           "up": 4, "down": 5, "left": 6, "right": 7, "l": 10, "r": 11}


class EmuInputError(RuntimeError):
    """The emulator could not be reached or driven."""


class EmuInput:
    def __init__(self, cdp_url: str, conversation_id: str | None = None,
                 check_stop: Callable[[], None] | None = None):
        if not cdp_url:
            raise EmuInputError("no emulator CDP endpoint")
        self._cdp_url = cdp_url
        self._conversation_id = conversation_id
        self._check_stop = check_stop or (lambda: None)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        daemon=True, name="emu-input")
        self._thread.start()
        self._pw = self._browser = None

    def _run(self, coro, timeout=20):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except Exception as exc:
            fut.cancel()
            raise EmuInputError(str(exc)) from exc

    async def _browser_h(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self._pw is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(
            self._cdp_url, no_defaults=True, timeout=5000)
        return self._browser

    async def _emu_page(self):
        owned = None
        if self._conversation_id is not None:
            import operator_browser_jobs
            helper = operator_browser_jobs.tabs()
            owned = set(helper._read_registry(helper.DEFAULT_REGISTRY).get(self._conversation_id, []))
            if not owned:
                raise EmuInputError("this chat has no owned browser tabs; open the game in this chat")
        browser = await self._browser_h()
        candidates = []
        for context in browser.contexts:
            for page in context.pages:
                if page.is_closed():
                    continue
                if owned is not None:
                    session = await context.new_cdp_session(page)
                    try:
                        info = await session.send("Target.getTargetInfo")
                        if info["targetInfo"]["targetId"] not in owned:
                            continue
                    finally:
                        await session.detach()
                try:
                    if await asyncio.wait_for(page.evaluate(
                            "!!(window.EJS_emulator && window.EJS_emulator.gameManager)"), 1.5):
                        candidates.append(page)
                except Exception:
                    continue
        if len(candidates) != 1:
            scope = "this chat" if owned is not None else "the sandbox browser"
            raise EmuInputError("expected one running EmulatorJS game in " + scope + "; found "
                                + str(len(candidates)) + ". Open one game or close the extra game tabs.")
        return candidates[0]

    @staticmethod
    def validate(buttons, hold_ms, gap_ms):
        if not isinstance(buttons, list) or len(buttons) > 8:
            raise EmuInputError("buttons must be a list of at most 8 controller buttons")
        if any(not isinstance(b, str) or b.lower() not in BUTTONS for b in buttons):
            raise EmuInputError("unknown controller button")
        if type(hold_ms) is not int or not 16 <= hold_ms <= 2000:
            raise EmuInputError("hold_ms must be an integer from 16 to 2000")
        if type(gap_ms) is not int or not 0 <= gap_ms <= 2000:
            raise EmuInputError("gap_ms must be an integer from 0 to 2000")
        if len(buttons) * (hold_ms + gap_ms) > 10000:
            raise EmuInputError("input sequence must finish within 10 seconds")
        return [b.lower() for b in buttons]

    async def _input_async(self, buttons, hold_ms, gap_ms):
        page = await self._emu_page()
        if buttons and not await page.evaluate(
                "!!(EJS_emulator.started && !EJS_emulator.paused)"):
            raise EmuInputError("Game is not running. Start or resume it before pressing buttons")
        check_stop = getattr(self, "_check_stop", lambda: None)

        async def wait(ms):
            remaining = ms / 1000
            while remaining > 0:
                check_stop()
                step = min(remaining, 0.025)
                await asyncio.sleep(step)
                remaining -= step
            check_stop()

        for button in buttons:
            check_stop()
            timer = None
            try:
                timer = await page.evaluate("""({id, hold}) => {
                    const gm = EJS_emulator.gameManager;
                    gm.simulateInput(0, id, 1);
                    // This timer survives an MCP process exit. Normal and STOP
                    // releases happen sooner in the host's finally block.
                    return setTimeout(() => gm.simulateInput(0, id, 0), hold + 250);
                }""", {"id": BUTTONS[button], "hold": hold_ms})
                await wait(hold_ms)
            finally:
                await page.evaluate("""({id, timer}) => {
                    clearTimeout(timer);
                    EJS_emulator.gameManager.simulateInput(0, id, 0);
                }""", {"id": BUTTONS[button], "timer": timer})
            await wait(gap_ms)
        result = await page.evaluate("""() => {
            const e = window.EJS_emulator, gm = e.gameManager;
            const store = window.__operatorCheckpoints;
            return {ok:true, core:window.EJS_core || null,
                    started:!!e.started, paused:!!e.paused, url:location.href,
                    save_state_available:typeof gm.getState === 'function',
                    load_state_available:typeof gm.loadState === 'function',
                    checkpoints:store && store.manager === gm ?
                        [...store.states.keys()].sort() : []};
        }""")
        result["pressed"] = buttons
        shot = await page.screenshot(type="png", timeout=5000)
        return result, shot

    async def _checkpoint_async(self, action, slot):
        page = await self._emu_page()
        getattr(self, "_check_stop", lambda: None)()
        info = await page.evaluate("""async ({action, slot}) => {
            const e = EJS_emulator, gm = e.gameManager;
            if (!e.started) throw new Error('Game has not started');
            let store = window.__operatorCheckpoints;
            if (!store || store.manager !== gm) {
                store = {manager:gm, states:new Map()};
                window.__operatorCheckpoints = store;
            }
            if (action === 'save_state') {
                if (typeof gm.getState !== 'function')
                    throw new Error('Save states are unavailable for this core');
                const state = await gm.getState();
                if (!(state instanceof Uint8Array) || !state.byteLength ||
                    state.byteLength > 16 * 1024 * 1024)
                    throw new Error('Checkpoint must contain 1 byte to 16 MiB');
                store.states.set(slot, new Uint8Array(state));
            } else {
                if (!store.states.has(slot)) throw new Error('Checkpoint slot is empty');
                if (typeof gm.loadState !== 'function')
                    throw new Error('Load states are unavailable for this core');
                await gm.loadState(new Uint8Array(store.states.get(slot)));
            }
            return {action, slot, size:store.states.get(slot).byteLength};
        }""", {"action": action, "slot": slot})
        result, shot = await self._input_async([], 16, 0)
        result.update(info)
        return result, shot

    def checkpoint(self, action, slot=0):
        if action not in ("save_state", "load_state"):
            raise EmuInputError("unknown checkpoint action")
        if type(slot) is not int or not 0 <= slot <= 3:
            raise EmuInputError("checkpoint slot must be an integer from 0 to 3")
        return self._run(self._checkpoint_async(action, slot))

    def input(self, buttons, hold_ms=120, gap_ms=700):
        buttons = self.validate(buttons, hold_ms, gap_ms)
        return self._run(self._input_async(buttons, hold_ms, gap_ms))

    async def _close(self):
        if self._pw is not None:
            await self._pw.stop()
        self._pw = self._browser = None

    def close(self):
        if not self._loop.is_running():
            return
        try:
            self._run(self._close(), timeout=3)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=3)
            if not self._thread.is_alive():
                self._loop.close()
