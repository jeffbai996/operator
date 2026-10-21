"""The tab-follow probes must not block the loop that publishes frames and runs input."""
import asyncio
import time
import urllib.request

import operator_view as OV
from test_capture_delivery import prepared


class _SlowList:
    def read(self):
        return b"[]"


def test_tab_follow_probes_do_not_stall_the_capture_event_loop(monkeypatch):
    def slow_urlopen(*args, **kwargs):
        time.sleep(0.3)             # a Chrome that is slow to list its targets
        return _SlowList()

    monkeypatch.setattr(urllib.request, "urlopen", slow_urlopen)
    s, _ = prepared()
    s._tab_check_ts = 0.0
    monkeypatch.setattr(OV, "_background_targets", lambda: set())

    async def scenario():
        gaps = []

        async def ticker():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        await s._follow_active_tab()
        await asyncio.sleep(0.03)   # let the ticker record any gap
        tick.cancel()
        return max(gaps)

    worst_gap = asyncio.run(scenario())
    assert worst_gap < 0.15, f"event loop stalled {worst_gap * 1000:.0f}ms on /json/list"
