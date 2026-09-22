"""Conversation state must survive a runner restart and a model change."""
import json

import pytest

import operator_agent as OA


@pytest.mark.parametrize("keep_resume", [True, False])
@pytest.mark.parametrize("task", ["Get me those times again", "Can you see the earlier transcript? That job"])
def test_restart_then_astra_keeps_booking_context(monkeypatch, tmp_path, keep_resume, task):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(OA, "_resolve_codex", lambda: "/fake/codex")
    monkeypatch.setattr(OA, "_squad_boot_context", lambda bot: "")
    monkeypatch.setitem(OA.AGENT_BOTS, "gpt", dict(
        OA.AGENT_BOTS["gpt"], config_dir=str(tmp_path / "codex")))
    monkeypatch.setattr(OA.AgentRunner, "_completion_gate_check", lambda self: "")
    monkeypatch.setattr(OA.AgentRunner, "_reap_owned_browser_helpers", lambda self: 0)
    first = OA.AgentRunner(conversation_id="booking")
    first._transcript = [
        {"role": "user", "text": "Find Cam Clark M2 service appointments."},
        {"role": "assistant", "text": "The earliest available is September 10 at 10:30."},
    ]
    first._last_bot = "gpt"
    if keep_resume:
        first._session_ids["gpt"] = "booking-native-thread"
    first._save_state()

    captured = []

    class Process:
        pid = 999999
        returncode = 0

        def __init__(self, cmd, **kwargs):
            captured.append(cmd)
            self.stdout = iter([json.dumps({
                "type": "thread.started", "thread_id": "booking-native-thread"})])

        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(OA.subprocess, "Popen", Process)
    restored = OA.AgentRunner(conversation_id="booking")
    assert restored.start("gpt", task, model="gpt-6-astra")["ok"]
    restored._thread.join(timeout=5)
    assert not restored._thread.is_alive()
    assert len(captured) == 1
    assert restored._browser_required == task.startswith("Get me")
    cmd = captured[0]
    assert cmd[cmd.index("-m") + 1] == "gpt-6-astra"
    if keep_resume:
        assert cmd[cmd.index("resume") + 1] == "booking-native-thread"
    else:
        assert "Cam Clark M2" in cmd[-1]
        assert "September 10 at 10:30" in cmd[-1]
    other = OA.AgentRunner(conversation_id="unrelated")
    assert other._transcript == []
    assert other._session_ids == {}
