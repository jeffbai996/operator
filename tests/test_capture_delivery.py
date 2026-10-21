"""Capture completion, producer pacing and numeric frame timing contracts."""
import asyncio
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
import pytest
import operator_view as OV
from test_streamer_lifecycle import FakeCtx, FakeBrowser, FakeSess


def prepared(primary=None):
    ctx=FakeCtx();ctx.sess=primary or FakeSess();s=OV._Streamer()
    s._browser=FakeBrowser(ctx);s._page=ctx.pages[0];s.view_w=1280;s.view_h=800
    return s,ctx


def test_retry_success_stamps_actual_tier():
    class FailsOnce(FakeSess):
        async def send(self, method, params=None):
            if method=="Page.captureScreenshot":raise RuntimeError("transient capture")
            return await super().send(method,params)
    s,ctx=prepared(FailsOnce());retry=FakeSess()
    async def new_session(page):
        return retry if s._cdp is not None else ctx.sess
    ctx.new_cdp_session=new_session
    s.tier="eco";s._capture_tier="lo"
    result=asyncio.run(s._grab(s._page))
    assert result==OV._PLACEHOLDER_JPEG
    assert s._capture_tier=="eco"


def test_optional_overlay_never_blocks_capture_pixels():
    class SlowOverlay(FakeSess):
        async def send(self, method, params=None):
            if method=="Runtime.evaluate" and "__opClick" in (params or {}).get("expression",""):
                await asyncio.sleep(60)
            return await super().send(method,params)
    s,_=prepared(SlowOverlay())
    async def scenario():
        result=await asyncio.wait_for(s._grab(s._page),.15)
        assert result==OV._PLACEHOLDER_JPEG
        assert not s._click_metadata_task
    asyncio.run(scenario())


def test_producer_wait_counts_elapsed_capture_and_preserves_idle_busy_limits():
    from operator_feed import producer_delay
    assert producer_delay(.066,.04,active=True,busy=False)==pytest.approx(.026)
    assert producer_delay(.066,.09,active=True,busy=False)==0
    assert producer_delay(.35,.04,active=False,busy=False)==.35
    assert producer_delay(.08,.04,active=True,busy=True)==.08


def test_published_snapshot_is_immutable_and_matches_pixels_and_identity():
    s,_=prepared();s._target_ids[s._page]="fixture"
    asyncio.run(s._grab(s._page));s._publish_frame(OV._PLACEHOLDER_JPEG)
    snapshot=s._frame_snapshot
    assert snapshot.data is s.frame
    assert snapshot.sequence==s.frame_id
    assert snapshot.source_tier==s._frame_tier
    assert snapshot.target_id=="fixture"
    assert snapshot.capture_started<=snapshot.capture_finished<=snapshot.published_at
    assert snapshot.width==1280 and snapshot.height==800
    with pytest.raises(FrozenInstanceError):snapshot.sequence=0


def test_clock_offset_bounds_and_timing_ring_are_bounded():
    import subprocess
    script=r"""
const assert=require('node:assert/strict'), feed=require(process.argv[1]);
const headers=new Headers({'X-Operator-Request-At':'10.1','X-Operator-Response-At':'10.2','X-Operator-Capture-Started':'10.05','X-Operator-Capture-Finished':'10.08'});
const transfer={response:{headers},startedAt:100,headersAt:300,bodyMs:10};
const sample=feed.presented(transfer,{paintAt:340,decodedAt:320,bytes:500});
assert(Math.abs(sample.captureToPaintMinMs-190)<.01);
assert(Math.abs(sample.captureToPaintMaxMs-290)<.01);
for(let i=0;i<100;i++)feed.presented(transfer,{paintAt:340,decodedAt:320,bytes:500});
assert.equal(feed.samples().length,60);
assert(Object.values(feed.samples()[0]).every(Number.isFinite));
"""
    result=subprocess.run(["node","-e",script,str(Path(__file__).parents[1]/"static/js/operator_feed.js")],capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stderr


def test_overlay_is_single_flight_and_teardown_cancels_it():
    class Pending(FakeSess):
        async def send(self, method, params=None):
            if method == "Runtime.evaluate":
                self.sent.append((method, params))
                await asyncio.sleep(60)
            return await super().send(method, params)
    async def scenario():
        s,_=prepared();session=Pending();s._metric_sessions[s._page]=session
        s._queue_click_metadata(s._page)
        task=s._click_metadata_task
        await asyncio.sleep(0)
        s._queue_click_metadata(s._page)
        assert s._click_metadata_task is task
        await s._teardown()
        assert task.cancelled()
        assert s._click_metadata_task is None
    asyncio.run(scenario())


def test_overlay_ignores_late_old_page_and_does_not_extend_duplicate_click():
    import json
    class Marker(FakeSess):
        async def send(self, method, params=None):
            await asyncio.sleep(0)
            return {"result":{"value":json.dumps({"x":.25,"y":.75,"t":123})}}
    async def scenario():
        s,ctx=prepared();page=s._page;s._metric_sessions[page]=Marker()
        s._queue_click_metadata(page)
        await s._click_metadata_task
        click=s.last_click
        assert click[:2]==(.25,.75)
        s._click_metadata_at=0;s._queue_click_metadata(page)
        await s._click_metadata_task
        assert s.last_click==click
        s._click_metadata_at=0;s._click_metadata_event=None
        s._queue_click_metadata(page);s._page=object()
        await s._click_metadata_task
        assert s.last_click==click
    asyncio.run(scenario())


def test_frame_headers_stay_with_snapshot_when_encoder_publishes_new_frame(monkeypatch):
    from flask import Flask
    s,_=prepared();s._target_ids[s._page]="before"
    asyncio.run(s._grab(s._page));s._publish_frame(OV._PLACEHOLDER_JPEG)
    snapshot=s._frame_snapshot
    app=Flask(__name__);app.register_blueprint(OV.bp)
    monkeypatch.setattr(OV,"_streamer",s)
    # The live cockpit, whatever mode an earlier module left the view in: the
    # read-only public demo answers this route 403 (red in file order since
    # 2026-10-19, green alone).
    monkeypatch.setattr(OV,"DEMO",False)
    monkeypatch.setitem(OV._active_surface,"name","browser")
    monkeypatch.setattr(OV,"_apply_feed_tier",lambda:None)
    monkeypatch.setattr(s,"ensure_running",lambda:None)
    def encode(data, tier, *, source_tier):
        s._target_ids[s._page]="after";s._capture_started_at+=1
        s._publish_frame(data+b"new")
        return data
    monkeypatch.setattr(OV._frame_encoder,"for_viewer",encode)
    response=app.test_client().get("/operator/frame?tier=lo")
    assert response.status_code==200
    assert response.data==snapshot.data
    assert response.headers["X-Operator-Target-ID"]==snapshot.target_id
    assert float(response.headers["X-Operator-Capture-Started"])==pytest.approx(snapshot.capture_started,abs=.000001)
    assert s._frame_snapshot.sequence>snapshot.sequence


def test_equal_capture_refreshes_timing_without_changing_pixel_identity():
    s,_=prepared();first=bytes(bytearray(OV._PLACEHOLDER_JPEG))
    s._publish_frame(first);sequence=s.frame_id
    second=bytes(bytearray(first));assert second is not first
    s._capture_started_at=time.monotonic();s._capture_finished_at=time.monotonic()
    assert not s._publish_frame(second)
    assert s.frame_id==sequence
    assert s._frame_snapshot.data is s.frame
    assert s._frame_snapshot.capture_started==s._capture_started_at
