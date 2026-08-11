"""Viewport ownership release — the letterbox that survived v1.0.33.

v1.0.33 ("the letterbox, root-caused") added stage_size ownership so a
background tab could not re-aspect the shared browser under the active viewer.
It also added the release valve: an owner that stops pulling frames for
VP_OWNER_IDLE_S gives the aspect up, so a fresh cockpit tab's load beacon is
honoured instead of the dead previous tab holding it forever.

The release valve never worked. `vp_beacon_allowed` bound the dict METHOD
instead of reading the owner's timestamp:

    ts = self._vp_seen.get                     # <-- the method object
    return ts is None or time.monotonic() - ts > VP_OWNER_IDLE_S

`ts` is then never None, so the comparison runs `float - builtin_function` and
raises TypeError on the ONLY path that can release a stale owner. Every
non-owner beacon dies there, the aspect is never handed over, and the stage
stays letterboxed — the exact symptom v1.0.33 set out to fix, still present at
1.0.35.
"""
import time

import operator_view


def _fresh():
    s = operator_view._Streamer()
    s._vp_owner = ""
    s._vp_seen = {}
    return s


def test_no_owner_allows_any_beacon():
    s = _fresh()
    assert s.vp_beacon_allowed("tab-a") is True


def test_owner_may_rebeacon_itself():
    s = _fresh()
    s._vp_owner = "tab-a"
    s._vp_seen["tab-a"] = time.monotonic()
    assert s.vp_beacon_allowed("tab-a") is True


def test_live_owner_blocks_another_viewer():
    """A second tab must not re-aspect the browser under someone watching."""
    s = _fresh()
    s._vp_owner = "tab-a"
    s._vp_seen["tab-a"] = time.monotonic()
    assert s.vp_beacon_allowed("tab-b") is False


def test_stale_owner_releases_to_a_new_viewer():
    """The regression: owner last pulled a frame well past the idle window, so
    a fresh tab's beacon must be honoured. This raised TypeError before."""
    s = _fresh()
    s._vp_owner = "tab-a"
    s._vp_seen["tab-a"] = time.monotonic() - (operator_view.VP_OWNER_IDLE_S + 5)
    assert s.vp_beacon_allowed("tab-b") is True


def test_owner_that_never_pulled_a_frame_releases():
    """An owner recorded with no pull timestamp cannot hold the aspect hostage."""
    s = _fresh()
    s._vp_owner = "ghost"
    assert s.vp_beacon_allowed("tab-b") is True
