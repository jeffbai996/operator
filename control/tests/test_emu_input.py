"""Exercise controller input against an isolated browser, never a live game."""
import asyncio
import os
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

import emu_input as E
import operator_browser_jobs


@pytest.mark.parametrize("buttons,hold,gap", [
    (["a", "invalid"], 120, 700), (["a"] * 9, 120, 700),
    (["a"], -1, 700), (["a"], 120, -1), (["a"] * 8, 2000, 2000),
])
def test_invalid_sequence_rejected_before_input(buttons, hold, gap):
    with pytest.raises(E.EmuInputError):
        E.EmuInput.validate(buttons, hold, gap)


def test_owned_game_buttons_release_and_screenshot(monkeypatch):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True,
                executable_path=os.environ.get("CC_TEST_CHROMIUM") or None)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.set_content("<canvas width=160 height=144></canvas>")
                await page.evaluate("""() => {
                    window.events = [];
                    window.EJS_core = 'gb';
                    window.EJS_emulator = {started:true, paused:false, gameManager:{
                        simulateInput:(player,button,value) => {
                            events.push([button,value]);
                            document.body.dataset.last = button + ':' + value;
                        }
                    }};
                }""")
                session = await context.new_cdp_session(page)
                target = (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
                await session.detach()
                registry = {"chat-a": [target], "chat-b": []}
                monkeypatch.setattr(operator_browser_jobs, "tabs", lambda: SimpleNamespace(
                    DEFAULT_REGISTRY=None, _read_registry=lambda _: registry))
                driver = object.__new__(E.EmuInput)
                driver._browser = browser
                driver._conversation_id = "chat-b"
                with pytest.raises(E.EmuInputError, match="no owned"):
                    await driver._emu_page()
                driver._conversation_id = "chat-a"
                result, png = await driver._input_async(["a", "b", "start"], 16, 0)
                assert result["pressed"] == ["a", "b", "start"]
                assert png.startswith(b"\x89PNG")
                assert await page.evaluate("events") == [[8,1],[8,0],[0,1],[0,0],[3,1],[3,0]]
                await page.evaluate("EJS_emulator.paused=true")
                status, _ = await driver._input_async([], 16, 0)
                assert status["paused"]
                with pytest.raises(Exception, match="not running"):
                    await driver._input_async(["a"], 16, 0)
                assert len(await page.evaluate("events")) == 6
                other = await context.new_page()
                await other.evaluate("window.EJS_emulator={gameManager:{}}")
                # An unowned second game must not be considered.
                assert await driver._emu_page() == page
                session = await context.new_cdp_session(other)
                second = (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
                await session.detach()
                registry["chat-a"].append(second)
                with pytest.raises(E.EmuInputError, match="found 2"):
                    await driver._emu_page()
            finally:
                await browser.close()
    asyncio.run(run())


def test_checkpoint_roundtrip_and_stop_releases_button():
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True,
                executable_path=os.environ.get("CC_TEST_CHROMIUM") or None)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.set_content("<canvas width=160 height=144></canvas>")
                await page.evaluate("""() => {
                    window.events=[]; window.state=new Uint8Array([1,2,3]);
                    window.EJS_emulator={started:true, paused:false, gameManager:{
                        getState:()=>state,
                        loadState:s=>{window.state=s;},
                        simulateInput:(p,b,v)=>events.push([b,v])
                    }};
                }""")
                driver = object.__new__(E.EmuInput)
                driver._browser = browser
                driver._conversation_id = None
                saved, _ = await driver._checkpoint_async("save_state", 2)
                assert saved["size"] == 3 and saved["checkpoints"] == [2]
                # Saving must copy bytes, rather than retain the mutable core buffer.
                await page.evaluate("state[0]=9")
                restored, _ = await driver._checkpoint_async("load_state", 2)
                assert restored["action"] == "load_state"
                assert await page.evaluate("Array.from(state)") == [1,2,3]
                await page.evaluate("state[0]=8")
                await driver._checkpoint_async("load_state", 2)
                assert await page.evaluate("Array.from(state)") == [1,2,3]
                with pytest.raises(Exception, match="empty"):
                    await driver._checkpoint_async("load_state", 0)
                await page.evaluate("state=new Uint8Array(16*1024*1024+1)")
                with pytest.raises(Exception, match="16 MiB"):
                    await driver._checkpoint_async("save_state", 2)
                # A failed overwrite must preserve the previous checkpoint.
                await driver._checkpoint_async("load_state", 2)
                assert await page.evaluate("Array.from(state)") == [1,2,3]
                checks = 0
                def stop_after_press():
                    nonlocal checks
                    checks += 1
                    if checks > 1:
                        raise E.EmuInputError("STOP")
                driver._check_stop = stop_after_press
                with pytest.raises(E.EmuInputError, match="STOP"):
                    await driver._input_async(["a", "b"], 2000, 700)
                assert await page.evaluate("events") == [[8,1],[8,0]]
                driver._check_stop = lambda: None
                original_evaluate = page.evaluate
                async def abandon_cleanup(expression, arg=None):
                    if "clearTimeout(timer)" in expression:
                        raise RuntimeError("controller disconnected")
                    return await original_evaluate(expression, arg)
                page.evaluate = abandon_cleanup
                with pytest.raises(RuntimeError, match="disconnected"):
                    await driver._input_async(["right"], 16, 0)
                page.evaluate = original_evaluate
                await page.wait_for_function("events.at(-1)[0] === 7 && events.at(-1)[1] === 0")
                assert await page.evaluate("events.slice(-2)") == [[7,1],[7,0]]
                await page.evaluate("EJS_emulator.gameManager={loadState:()=>{}}")
                with pytest.raises(Exception, match="empty"):
                    await driver._checkpoint_async("load_state", 2)
                await page.reload()
                assert await page.evaluate("typeof window.__operatorCheckpoints") == "undefined"
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize("slot", [-1, 4, True, "0"])
def test_bad_checkpoint_slot_never_contacts_browser(slot):
    driver = object.__new__(E.EmuInput)
    with pytest.raises(E.EmuInputError, match="slot"):
        driver.checkpoint("save_state", slot)
