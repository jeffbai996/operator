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


def test_service_stop_waits_for_operator_to_be_idle(tmp_path, monkeypatch):
    marker = tmp_path / "deploy-drain"
    monkeypatch.setenv("OPERATOR_DRAIN_PATH", str(marker))
    payloads = [
        {"admission": {"active": 1,
                       "jobs": [{"conversation_id": "conv-a", "bot": "gemma"}]}},
        {"admission": {"active": 0, "jobs": []}},
    ]
    sleeps = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def open_state(_url, timeout):
        assert timeout <= 5
        return Response(payloads.pop(0))

    result = guard.wait_until_idle(
        "http://operator.test/agent", opener=open_state,
        sleep=lambda delay: sleeps.append(delay))

    assert result is True
    assert sleeps, "an active run must delay the service stop"
    assert marker.exists(), "the marker stays through the stop/start gap"
