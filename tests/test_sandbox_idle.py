"""Idle stop for the sandbox container.

The container is a full desktop that otherwise runs around the clock, so it is
paused (never deleted) once no client has used it for a while. `ensure()` already
restarts a stopped container on the next use.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_sandbox_idle.py -q
"""
import importlib.util
import pathlib
import time
import types

import pytest

_here = pathlib.Path(__file__).resolve()
for _cand in (_here.parents[1] / "computer-use" / "sandbox_container.py",
              _here.parents[2] / "computer-use" / "sandbox_container.py"):
    if _cand.exists():
        _p = _cand
        break
_spec = importlib.util.spec_from_file_location("sandbox_container_idle", _p)
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)

IDLE = 900.0


def due(**over):
    args = dict(running=True, live_clients=0, last_used=1000.0,
                now=1000.0 + IDLE, idle_after=IDLE)
    args.update(over)
    return sb.idle_stop_due(**args)


def test_stops_once_idle_window_has_passed_with_no_clients():
    assert due() is True


def test_waits_while_inside_the_idle_window():
    assert due(now=1000.0 + IDLE - 1) is False


def test_never_stops_while_a_client_is_attached():
    assert due(live_clients=1, now=1000.0 + 10 * IDLE) is False


def test_nothing_to_stop_when_container_is_not_running():
    assert due(running=False, now=1000.0 + 10 * IDLE) is False


def test_zero_window_disables_idle_stop():
    assert due(idle_after=0, now=1000.0 + 10 * IDLE) is False


def test_counts_only_docker_exec_sessions_for_this_container():
    c = sb.CONTAINER
    argvs = [
        ["/usr/bin/docker", "exec", "-i", "-u", "opuser", c, "sh"],
        ["docker", "exec", "-u", "opuser", "-e", "DISPLAY=:1", c, "ffmpeg"],
        ["/usr/bin/docker", "exec", "other-container", "sh"],
        ["/usr/bin/docker", "inspect", c],
        ["/usr/bin/docker", "stop", c],
        ["vim", c],
        [],
    ]
    assert sb.count_exec_clients(argvs) == 2


def test_unknown_client_state_counts_as_attached(monkeypatch):
    monkeypatch.setattr(sb.os.path, "isdir", lambda p: False if p == "/proc" else True)
    assert sb.live_clients() > 0


def test_marker_roundtrip_and_throttle(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "_USED_FILE", tmp_path / "used")
    monkeypatch.setattr(sb, "_last_marked", 0.0)
    clock = iter([100.0, 101.0, 200.0])
    # scoped to the module under test; patching time.time itself would hit pytest
    monkeypatch.setattr(sb, "time", types.SimpleNamespace(
        time=lambda: next(clock), sleep=time.sleep))
    sb.mark_used()
    assert sb.last_used() == 100.0
    sb.mark_used()                       # inside the throttle window: not rewritten
    assert sb.last_used() == 100.0
    sb.mark_used()
    assert sb.last_used() == 200.0


def test_missing_marker_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "_USED_FILE", tmp_path / "absent")
    assert sb.last_used() is None


@pytest.fixture
def rig(tmp_path, monkeypatch):
    calls = {"stop": 0}
    monkeypatch.setattr(sb, "_USED_FILE", tmp_path / "used")
    monkeypatch.setattr(sb, "_last_marked", 0.0)
    monkeypatch.setattr(sb, "IDLE_STOP_AFTER_S", IDLE)
    monkeypatch.setattr(sb, "state", lambda: "running")
    monkeypatch.setattr(sb, "live_clients", lambda: 0)
    monkeypatch.setattr(sb, "stop", lambda: calls.__setitem__("stop", calls["stop"] + 1))
    return calls


def test_reap_stops_an_idle_container(rig):
    (sb._USED_FILE).write_text("")
    import os
    os.utime(sb._USED_FILE, (1000.0, 1000.0))
    assert sb.reap_if_idle(now=1000.0 + IDLE) is True
    assert rig["stop"] == 1


def test_reap_leaves_a_recently_used_container(rig):
    import os
    sb._USED_FILE.write_text("")
    os.utime(sb._USED_FILE, (1000.0, 1000.0))
    assert sb.reap_if_idle(now=1000.0 + IDLE - 5) is False
    assert rig["stop"] == 0


def test_reap_leaves_a_container_with_an_attached_client(rig, monkeypatch):
    import os
    sb._USED_FILE.write_text("")
    os.utime(sb._USED_FILE, (1000.0, 1000.0))
    monkeypatch.setattr(sb, "live_clients", lambda: 1)
    assert sb.reap_if_idle(now=1000.0 + 10 * IDLE) is False
    assert rig["stop"] == 0


def test_first_check_without_a_marker_starts_the_window_instead_of_stopping(rig):
    assert sb.last_used() is None
    assert sb.reap_if_idle(now=5000.0) is False
    assert rig["stop"] == 0
    assert sb.last_used() is not None


def test_ensure_refreshes_the_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "_USED_FILE", tmp_path / "used")
    monkeypatch.setattr(sb, "_last_marked", 0.0)
    monkeypatch.setattr(sb, "state", lambda: "running")
    monkeypatch.setattr(sb, "_display_live", lambda: True)
    monkeypatch.setattr(sb, "_retire_legacy_cdp_bridge", lambda: None)
    monkeypatch.setattr(sb, "_ensure_host_bridge", lambda: None)
    assert sb.last_used() is None
    sb.ensure()
    assert sb.last_used() is not None


def test_reaper_thread_is_a_single_daemon(monkeypatch):
    monkeypatch.setattr(sb, "IDLE_STOP_AFTER_S", IDLE)
    monkeypatch.setattr(sb, "reap_if_idle", lambda now=None: False)
    monkeypatch.setattr(sb, "_reaper", None)
    first = sb.start_idle_reaper(interval=3600)
    try:
        assert first is not None and first.daemon and first.is_alive()
        assert sb.start_idle_reaper(interval=3600) is first
    finally:
        sb.stop_idle_reaper()
    assert not first.is_alive()


def test_reaper_does_not_start_when_disabled(monkeypatch):
    monkeypatch.setattr(sb, "IDLE_STOP_AFTER_S", 0.0)
    monkeypatch.setattr(sb, "_reaper", None)
    assert sb.start_idle_reaper() is None
