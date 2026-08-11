import json
import time

import operator_restart_guard as guard


def test_detects_only_direct_squad_store_restart():
    assert guard.requests_server_restart(
        "systemctl --user restart host-app-server.service")
    assert guard.requests_server_restart(
        "cd /tmp && systemctl --user restart host-app-server")
    assert not guard.requests_server_restart(
        "systemctl --user status host-app-server.service")
    assert not guard.requests_server_restart(
        "systemctl --user restart gpt.service")


def test_deferred_restart_is_coalesced_and_consumed_once(tmp_path, monkeypatch):
    marker = tmp_path / "pending.json"
    monkeypatch.setenv("OPERATOR_RESTART_MARKER", str(marker))
    guard.defer("first")
    guard.defer("second")
    assert json.loads(marker.read_text())["command"] == "second"

    calls = []
    assert guard.consume_and_restart(run=lambda *a, **kw: calls.append((a, kw)))
    assert not guard.consume_and_restart(run=lambda *a, **kw: calls.append((a, kw)))
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert calls[0][0][0] == ["systemctl", "--user", "restart", guard.UNIT]


def test_no_pending_restart_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_RESTART_MARKER", str(tmp_path / "missing.json"))
    assert not guard.consume_and_restart()
