"""Bounded delivery and input ownership contracts; no live browser or agent."""
import asyncio
import io
import threading
import time
from pathlib import Path

from PIL import Image
import pytest
import operator_view as OV


def test_waiting_action_is_cancelled_before_it_can_execute(monkeypatch):
    s = OV._Streamer()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    s._loop = loop
    s._running = True
    s._page = object()
    monkeypatch.setattr(OV, "ACTION_TIMEOUT_S", .05)
    calls = []
    async def setup():
        await s._iolock().acquire()
    asyncio.run_coroutine_threadsafe(setup(), loop).result(1)
    async def dims(page):
        calls.append(page)
        return {"w": 100, "h": 100}
    monkeypatch.setattr(s, "_viewport_dims", dims)
    try:
        result = s.run_action({"kind": "move", "x": .5, "y": .5})
        assert result["outcome"] == "cancelled_before_dispatch"
        loop.call_soon_threadsafe(s._iolock().release)
        asyncio.run_coroutine_threadsafe(asyncio.sleep(.05), loop).result(1)
        assert calls == []
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(1)
        loop.close()


def test_target_switch_while_waiting_refuses_stale_coordinates():
    async def scenario():
        s = OV._Streamer()
        old, new = object(), object()
        s._page = old
        await s._iolock().acquire()
        action = asyncio.create_task(s._do_action({"kind": "move"}))
        await asyncio.sleep(0)
        s._page = new
        s._iolock().release()
        result = await action
        assert result["outcome"] == "target_changed"
        assert not result["ok"]
    asyncio.run(scenario())


def test_running_action_timeout_does_not_claim_it_was_never_sent(monkeypatch):
    s = OV._Streamer()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    s._loop, s._running = loop, True
    monkeypatch.setattr(OV, "ACTION_TIMEOUT_S", .05)
    async def hung(action):
        action["_dispatch_state"]["started"] = True
        await asyncio.sleep(60)
    monkeypatch.setattr(s, "_do_action", hung)
    try:
        result = s.run_action({"kind": "key", "value": "Enter"})
        assert result["outcome"] == "outcome_unknown"
        assert not result["ok"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(1)
        loop.close()


def test_quality_is_per_viewer_and_cache_is_bounded():
    from operator_feed import FrameEncoder
    image = Image.new("RGB", (1600, 1000), "white")
    stream = io.BytesIO()
    image.save(stream, "JPEG", quality=60)
    data = stream.getvalue()
    encoder = FrameEncoder()
    assert encoder.for_viewer(data, "hi") is data
    low = encoder.for_viewer(data, "eco")
    assert Image.open(io.BytesIO(low)).size == (640, 400)
    assert len(low) < len(data)
    assert encoder.for_viewer(data, "eco") is low
    for i in range(8):
        stream = io.BytesIO()
        Image.new("RGB", (1200, 800), (i, 20, 30)).save(stream, "JPEG")
        encoder.for_viewer(stream.getvalue(), "lo")
    assert len(encoder._cache) <= 2


def test_control_chooses_only_owned_target_and_fails_closed(monkeypatch, tmp_path):
    from operator_browser_target import tabs
    from control.surfaces import BrowserSurface, SurfaceError
    registry = tmp_path / "tabs.json"
    registry.write_text('{"alpha":["A","popup"],"bravo":["B"]}')
    monkeypatch.setattr(tabs(), "DEFAULT_REGISTRY", registry)
    monkeypatch.setenv("OPERATOR_CONVERSATION_ID", "alpha")
    class Session:
        def __init__(self, target): self.target = target
        async def send(self, *_): return {"targetInfo": {"targetId": self.target}}
        async def detach(self): pass
    class Context:
        async def new_cdp_session(self, page): return Session(page.target)
    class Page:
        def __init__(self, target): self.target, self.context = target, ctx
        def is_closed(self): return False
        async def evaluate(self, *_): return "visible"
    ctx = Context()
    foreign, owned, popup = [Page(t) for t in ("B", "A", "popup")]
    ctx.pages = [foreign, owned, popup]
    s = BrowserSurface.__new__(BrowserSurface)
    s._browser = type("Browser", (), {"contexts": [ctx]})()
    s._page = None
    s._sess = None
    async def no_media(): pass
    monkeypatch.setattr(s, "_force_dark_media", no_media)
    async def scenario():
        await s._pick_page()
        assert s._page is owned
        ctx.pages = [foreign, popup]
        await s._pick_page()
        assert s._page is popup
        ctx.pages = [foreign]
        with pytest.raises(SurfaceError, match="owned"):
            await s._pick_page()
    asyncio.run(scenario())


def test_feed_budget_and_hung_body_recovery():
    import subprocess
    script = r"""
const assert = require('node:assert/strict');
const feed = require(process.argv[1]);
assert.equal(feed.delay({tier:'hi',elapsed:100,bytes:1000}),0);
assert.equal(feed.delay({tier:'eco',elapsed:100,bytes:1000}),160);
assert.equal(feed.delay({tier:'hi',elapsed:100,bytes:1000,typing:true}),300);
assert.equal(feed.delay({tier:'hi',elapsed:20,quiet:true}),880);
for (const tier of ['hi','lo','eco']) {
  let elapsed=0, bytes=0, count=0; const size=300000;
  while(elapsed<5000) {bytes+=size;count++;elapsed+=20+feed.delay({tier,elapsed:20,bytes:size});}
  assert(bytes<=feed.BYTES_PER_SECOND[tier]*5+size);
  assert(count<=76);
}
(async()=>{
 let aborted=false;
 const hung=async (_url,options)=>({ok:true,status:200,headers:new Headers(),blob:()=>new Promise((_,reject)=>options.signal.addEventListener('abort',()=>{aborted=true;reject(Error('aborted'));}))});
 await assert.rejects(feed.read('fixture',{fetchImpl:hung,timeoutMs:20}),/deadline/);
 assert(aborted);
 const success=async ()=>({ok:true,status:200,headers:new Headers({'X-Operator-Frame-ID':'new'}),blob:async()=>new Blob(['pixels'])});
 assert.equal((await feed.read('fixture',{fetchImpl:success})).blob.size,6);
})();
"""
    path = Path(__file__).parents[1] / "static/js/operator_feed.js"
    result = subprocess.run(["node", "-e", script, str(path)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_control_endpoint_matches_cockpit_in_every_runtime(monkeypatch, tmp_path):
    import json
    import operator_runtimes as RT
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPERATOR_DEMO_CDP", "http://fixture:9333")
    spec = RT.RunSpec(binpath="/fake/bin", bot="fixture", task="test", persona="", boot_context="", model="", effort="", surface="browser", demo=False, real_ok=False, resume_id="", config_dir=str(tmp_path),conversation_id="alpha")
    codex = RT.build_cmd("codex", spec)
    assert 'mcp_servers.operator-control.env.OPERATOR_CDP="http://fixture:9333"' in codex.cmd
    claude = RT.build_cmd("claude", spec)
    servers = json.load(open(claude.mcp_config_path))["mcpServers"]
    assert servers["operator-control"]["env"]["OPERATOR_CDP"] == "http://fixture:9333"
    agy = RT.build_cmd("agy", spec)
    assert agy.env["OPERATOR_CDP"] == "http://fixture:9333"


def test_frame_ownership_is_stamped_at_publication_not_read_time():
    s = OV._Streamer()
    old, new = object(), object()
    s._target_ids = {old: "old", new: "new"}
    s._page = old
    s._publish_frame(b"pixels")
    s._page = new
    assert s._frame_target_id == "old"
    prior = s.frame_id
    assert s._publish_frame(b"pixels") is True
    assert s.frame_id == prior + 1
    assert s._frame_target_id == "new"


def test_viewer_quality_has_distinct_identity_for_same_source():
    source = type("Source", (), {"frame_id": 7})()
    assert OV._frame_token("browser", source, "eco") != OV._frame_token("browser", source, "hi")


def test_target_id_mismatch_refuses_before_execution():
    async def scenario():
        s=OV._Streamer(); s._page=object(); s._target_ids[s._page]="current"
        result=await s._do_action({"kind":"move","target_id":"old"})
        assert result["outcome"] == "target_changed"
    asyncio.run(scenario())


def test_frame_identity_changes_across_worker_instances():
    assert OV._frame_token("browser", OV._Streamer()) != OV._frame_token("browser", OV._Streamer())


def test_control_cannot_inject_after_ownership_release(monkeypatch, tmp_path):
    from operator_browser_target import tabs
    from control.surfaces import BrowserSurface, SurfaceError
    registry=tmp_path/"tabs.json";registry.write_text('{}')
    monkeypatch.setattr(tabs(),"DEFAULT_REGISTRY",registry)
    monkeypatch.setenv("OPERATOR_CONVERSATION_ID","alpha")
    s=BrowserSurface.__new__(BrowserSurface)
    s._page=type("Page",(),{"is_closed":lambda self:False})()
    s._target_ids={s._page:"previously-owned"}
    async def scenario():
        with pytest.raises(SurfaceError,match="owned"):
            await s._send("Input.insertText", {"text":"fixture"})
    asyncio.run(scenario())


def test_held_key_repeat_stops_when_target_changes():
    async def scenario():
        calls=[]
        class Keyboard:
            async def press(self, key): calls.append(key)
        old=type("Page",(),{"keyboard":Keyboard()})()
        new=type("Page",(),{"keyboard":Keyboard()})()
        s=OV._Streamer();s._page=old
        task=asyncio.create_task(s._repeat_key("ArrowDown"))
        await asyncio.sleep(0)
        s._page=new
        await asyncio.sleep(.35)
        task.cancel();await task
        assert calls==[]
    asyncio.run(scenario())


def test_native_quality_frame_is_not_lossily_encoded_twice():
    from operator_feed import FrameEncoder
    data=b"already encoded by Chrome"
    assert FrameEncoder().for_viewer(data,"eco",source_tier="eco") is data


@pytest.mark.parametrize("production_deadline", [False, True])
def test_key_release_cancels_repeat_before_contended_lock(monkeypatch, production_deadline):
    calls=[]
    class Keyboard:
        async def press(self,key):calls.append(key)
    page=type("Page",(),{"keyboard":Keyboard(),"url":"fixture"})()
    s=OV._Streamer();s._page=page;s._target_ids[page]="fixture"
    loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True)
    thread.start();s._loop=loop;s._running=True
    if not production_deadline:monkeypatch.setattr(OV,"ACTION_TIMEOUT_S",.05)
    async def begin():
        await s._do_action({"kind":"key_down","value":"ArrowDown","target_id":"fixture"})
        await s._iolock().acquire()
    asyncio.run_coroutine_threadsafe(begin(),loop).result(1)
    try:
        if production_deadline:loop.call_soon_threadsafe(loop.call_later,6.15,s._iolock().release)
        started=time.monotonic()
        result=s.run_action({"kind":"key_up","value":"ArrowDown","target_id":"fixture"})
        assert result["ok"],result
        assert time.monotonic()-started < .5
        assert not s._key_repeat
        async def observe():
            if not production_deadline:s._iolock().release()
            await asyncio.sleep(6.55 if production_deadline else .4)
            assert calls==["ArrowDown"]
        asyncio.run_coroutine_threadsafe(observe(),loop).result(8)
    finally:
        async def finish():
            tasks=list((s._key_repeat or {}).values())
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            if s._iolock().locked():s._iolock().release()
        asyncio.run_coroutine_threadsafe(finish(),loop).result(1)
        loop.call_soon_threadsafe(loop.stop);thread.join(1);loop.close()


def test_key_released_while_queued_presses_once_without_repeat():
    async def scenario():
        calls=[]
        class Keyboard:
            async def press(self,key):calls.append(key)
        page=type("Page",(),{"keyboard":Keyboard(),"url":"fixture"})()
        s=OV._Streamer();s._page=page;s._target_ids[page]="fixture"
        await s._iolock().acquire()
        down=asyncio.create_task(s._do_action({"kind":"key_down","value":"ArrowDown","target_id":"fixture"}))
        await asyncio.sleep(0)
        up=await asyncio.wait_for(s._do_action({"kind":"key_up","value":"ArrowDown","target_id":"fixture"}),.1)
        assert up["ok"]
        s._iolock().release()
        # A quick tap whose release beat the capture lock is still one press.
        assert (await down)["ok"]
        assert calls==["ArrowDown"] and not s._key_repeat
        await s._do_action({"kind":"key_down","value":"ArrowDown","target_id":"fixture"})
        assert calls==["ArrowDown","ArrowDown"]
        await s._do_action({"kind":"key_up","value":"ArrowDown","target_id":"fixture"})
        await asyncio.sleep(0)
        assert not s._key_repeat
    asyncio.run(scenario())


def test_stale_target_release_does_not_cancel_new_target_repeat():
    async def scenario():
        class Keyboard:
            async def press(self,key):pass
        old=type("Page",(),{"keyboard":Keyboard(),"url":"old"})()
        new=type("Page",(),{"keyboard":Keyboard(),"url":"new"})()
        s=OV._Streamer();s._page=old;s._target_ids={old:"old",new:"new"}
        await s._do_action({"kind":"key_down","value":"ArrowDown","target_id":"old"})
        old_task=s._key_repeat["ArrowDown"]
        await asyncio.sleep(0)  # enter the old task so cancellation runs its finally
        s._page=new
        await s._do_action({"kind":"key_down","value":"ArrowDown","target_id":"new"})
        new_task=s._key_repeat["ArrowDown"]
        await asyncio.sleep(0)
        assert s._key_repeat.get("ArrowDown") is new_task
        result=await s._do_action({"kind":"key_up","value":"ArrowDown","target_id":"old"})
        assert result["outcome"]=="target_changed"
        assert s._key_repeat.get("ArrowDown") is new_task and not new_task.done()
        await s._do_action({"kind":"key_up","value":"ArrowDown","target_id":"new"})
        await asyncio.gather(old_task,new_task,return_exceptions=True)
        assert old_task.done() and new_task.done() and not s._key_repeat
    asyncio.run(scenario())


def test_teardown_stops_repeat_waiting_for_input_lock():
    async def scenario():
        calls=[]
        class Keyboard:
            async def press(self,key):calls.append(key)
        page=type("Page",(),{"keyboard":Keyboard(),"url":"fixture"})()
        s=OV._Streamer();s._page=page
        await s._do_action({"kind":"key_down","value":"ArrowDown"})
        task=s._key_repeat["ArrowDown"]
        await s._iolock().acquire();await asyncio.sleep(.3)
        await asyncio.wait_for(s._teardown(),.2)
        assert task.done() and not s._key_repeat
        s._iolock().release();await asyncio.sleep(.05)
        assert calls==["ArrowDown"]
    asyncio.run(scenario())


@pytest.mark.parametrize("finish", ["release", "teardown", "teardown-twice"])
def test_release_finishes_inflight_repeat_pair_without_another_tick(finish):
    async def scenario():
        events=[];started=asyncio.Event();complete=asyncio.Event()
        class Keyboard:
            count=0
            async def press(self,key):
                self.count+=1;events.append("down")
                if self.count==2:
                    started.set();await complete.wait()
                events.append("up")
        page=type("Page",(),{"keyboard":Keyboard(),"url":"fixture"})()
        s=OV._Streamer();s._page=page;s._target_ids[page]="fixture"
        await s._do_action({"kind":"key_down","value":"ArrowDown","target_id":"fixture"})
        repeat=s._key_repeat["ArrowDown"]
        await asyncio.wait_for(started.wait(),.5)
        if finish=="release":
            result=await s._do_action({"kind":"key_up","value":"ArrowDown","target_id":"fixture"})
            assert result["ok"] and not s._key_repeat
            await asyncio.sleep(0)
            teardown=asyncio.create_task(s._teardown())
        else:
            teardown=asyncio.create_task(s._teardown())
        await asyncio.sleep(0)
        if finish=="teardown-twice":
            second_teardown=asyncio.create_task(s._teardown())
            await asyncio.sleep(0)
        assert not teardown.done()
        complete.set();await asyncio.wait_for(teardown,.2)
        if finish=="teardown-twice":await asyncio.wait_for(second_teardown,.2)
        await asyncio.wait_for(repeat,.2)
        await asyncio.sleep(.06)
        assert events==["down","up","down","up"]
    asyncio.run(scenario())


def test_cancelled_repeat_releases_native_key_if_pair_cannot_finish(monkeypatch):
    original_wait=asyncio.wait_for
    async def short_wait(awaitable, timeout):
        return await original_wait(awaitable,.05 if timeout==1 else timeout)
    monkeypatch.setattr(asyncio,"wait_for",short_wait)
    async def scenario():
        events=[];started=asyncio.Event()
        class Keyboard:
            count=0
            async def press(self,key):
                self.count+=1;events.append("down")
                if self.count==2:
                    started.set();await asyncio.sleep(60)
                events.append("up")
            async def up(self,key):events.append("cleanup-up")
        page=type("Page",(),{"keyboard":Keyboard(),"url":"fixture"})()
        s=OV._Streamer();s._page=page;s._target_ids[page]="fixture"
        await s._do_action({"kind":"key_down","value":"ArrowDown","target_id":"fixture"})
        repeat=s._key_repeat["ArrowDown"]
        await original_wait(started.wait(),.5)
        assert (await s._do_action({"kind":"key_up","value":"ArrowDown","target_id":"fixture"}))["ok"]
        await original_wait(repeat,.3)
        assert events==["down","up","down","cleanup-up"]
        assert not s._key_repeat
    asyncio.run(scenario())


def test_run_action_closes_unstarted_coroutine_without_runtime_warning(monkeypatch):
    import gc
    import warnings
    monkeypatch.setattr(OV, "ACTION_TIMEOUT_S", 0.05)
    s = OV._Streamer()
    monkeypatch.setattr(s, "ensure_running", lambda: None)
    monkeypatch.setattr(s, "_force_reattach", lambda: None)

    class StoppedLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, _cb):
            # Simulates loop stopping between is_running() and callback execution
            pass

    s._loop = StoppedLoop()
    s._running = True
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        res = s.run_action({"kind": "move", "x": 0.5, "y": 0.5})
        del res
        gc.collect()
    unawaited = [w for w in caught if "bounded_action" in str(w.message)]
    assert not unawaited, [str(w.message) for w in unawaited]

