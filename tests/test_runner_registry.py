"""Conversation-scoped runners and shared admission."""
from __future__ import annotations

import types
import sys
import threading
import subprocess
import time
from pathlib import Path

import operator_agent as OA


class _FakeRunner:
    def __init__(self, conversation_id: str, **_kwargs) -> None:
        self.conversation_id = conversation_id
        self.bot = None
        self.task = None
        self.running = False
        self.messages = []
        self.stopped = False

    def is_running(self) -> bool:
        return self.running

    def start(self, bot: str, task: str, **_kwargs) -> dict:
        self.bot, self.task, self.running = bot, task, True
        return {"ok": True, "bot": bot}

    def stop(self) -> dict:
        self.stopped = True
        self.running = False
        return {"ok": True}

    def snapshot(self, since: float = 0.0) -> dict:
        return {"state": "running" if self.running else "idle",
                "bot": self.bot, "messages": list(self.messages), "since": since}

    def steer(self, text: str) -> dict:
        return {"ok": self.running, "text": text}

    def reset_session(self, bot: str = "") -> dict:
        return {"ok": True, "bot": bot}


def _registry(**kwargs):
    return OA.RunnerRegistry(runner_factory=_FakeRunner, global_limit=2,
                             per_bot_limit=1, **kwargs)


def test_two_conversations_run_in_isolated_runners() -> None:
    runners = _registry()
    assert runners.start("claude-a", "one", conversation_id="conv-a")["ok"]
    assert runners.start("gemma", "two", conversation_id="conv-b")["ok"]

    a = runners.get("conv-a")
    b = runners.get("conv-b")
    assert a is not b
    a.messages.append({"text": "only a"})
    assert b.messages == []
    assert runners.admission_snapshot()["active"] == 2


def test_third_dispatch_names_requested_bot_and_slot_owners() -> None:
    runners = _registry()
    runners.start("claude-a", "one", conversation_id="conv-a")
    runners.start("gemma", "two", conversation_id="conv-b")

    denied = runners.start("gpt", "three", conversation_id="conv-c")
    assert denied["ok"] is False
    assert "requested bot 'gpt'" in denied["error"]
    assert "global limit 2" in denied["error"]
    assert "conversation 'conv-a'" in denied["error"]
    assert "conversation 'conv-b'" in denied["error"]


def test_one_bot_cannot_contend_for_its_working_directory() -> None:
    runners = _registry()
    runners.start("claude-a", "one", conversation_id="conv-a")
    denied = runners.start("claude-a", "two", conversation_id="conv-b")
    assert denied["ok"] is False
    assert "requested bot 'claude-a'" in denied["error"]
    assert "conversation 'conv-a' holds the claude-a slot" in denied["error"]


def test_stop_and_telemetry_are_per_conversation() -> None:
    runners = _registry()
    runners.start("claude-a", "one", conversation_id="conv-a")
    runners.start("gemma", "two", conversation_id="conv-b")

    assert runners.stop(conversation_id="conv-a")["ok"]
    assert runners.get("conv-a").stopped is True
    assert runners.get("conv-b").is_running() is True
    assert runners.snapshot(12.5, conversation_id="conv-b")["bot"] == "gemma"
    assert runners.snapshot(12.5, conversation_id="conv-b")["conversation_id"] == "conv-b"


def test_conversation_summaries_feed_the_thread_switcher() -> None:
    runners = _registry()
    runners.start("claude-a", "one", conversation_id="conv-a")
    runners.start("gemma", "two", conversation_id="conv-b")

    statuses = runners.conversation_summaries()
    assert statuses["conv-a"] == {"state": "running", "bot": "claude-a",
                                  "alive": True}
    assert statuses["conv-b"] == {"state": "running", "bot": "gemma",
                                  "alive": True}


def test_finished_runner_releases_room_for_another_conversation() -> None:
    runners = _registry()
    runners.start("claude-a", "one", conversation_id="conv-a")
    runners.start("gemma", "two", conversation_id="conv-b")
    runners.get("conv-a").running = False
    assert runners.start("gpt", "three", conversation_id="conv-c")["ok"]


def test_deployment_drain_refuses_a_new_dispatch(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "operator-deploy-drain"
    marker.write_text("deploying\n")
    monkeypatch.setenv("OPERATOR_DRAIN_PATH", str(marker))

    denied = _registry().start("gemma", "new task", conversation_id="conv-a")

    assert denied["ok"] is False
    assert "requested bot 'gemma'" in denied["error"]
    assert "deployment" in denied["error"]


def test_pre_spawn_reservation_cannot_be_reaped_by_a_racing_dispatch() -> None:
    entered = threading.Event()
    release = threading.Event()

    class _StartingRunner(_FakeRunner):
        def start(self, bot: str, task: str, **_kwargs) -> dict:
            entered.set()
            assert release.wait(2)
            return super().start(bot, task, **_kwargs)

    def factory(conversation_id: str, **kwargs):
        cls = _StartingRunner if conversation_id == "conv-a" else _FakeRunner
        return cls(conversation_id, **kwargs)

    runners = OA.RunnerRegistry(runner_factory=factory, global_limit=1,
                                per_bot_limit=1)
    first = threading.Thread(target=lambda: runners.start(
        "claude-a", "one", conversation_id="conv-a"))
    first.start()
    assert entered.wait(1)
    threading.Timer(0.1, release.set).start()
    try:
        denied = runners.start("gemma", "two", conversation_id="conv-b")
        assert denied["ok"] is False
        assert "global limit 1" in denied["error"]
    finally:
        first.join(2)


def test_agent_runner_paths_are_isolated_by_conversation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPERATOR_SESSION_ROOT", str(tmp_path / "work"))
    one = OA.AgentRunner(conversation_id="conv-a")
    two = OA.AgentRunner(conversation_id="conv-b")
    assert one._state_path != two._state_path
    assert one.cwd_for("claude-a") != two.cwd_for("claude-a")
    assert "conv-a" in one.cwd_for("claude-a")
    assert "conv-b" in two.cwd_for("claude-a")


def test_scoped_claude_cwd_keeps_the_base_instruction_file(
        tmp_path, monkeypatch) -> None:
    base = tmp_path / "claude-a"
    base.mkdir()
    instructions = base / "CLAUDE.md"
    instructions.write_text("operator instructions")
    monkeypatch.setitem(OA.AGENT_BOTS["claude-a"], "cwd", str(base))
    runner = OA.AgentRunner(conversation_id="conv-a")
    scoped = runner.cwd_for("claude-a")
    linked = Path(scoped) / "CLAUDE.md"
    assert linked.is_symlink()
    assert linked.read_text() == "operator instructions"


def test_busy_runner_error_names_request_and_owner(monkeypatch) -> None:
    runner = OA.AgentRunner(conversation_id="conv-a")
    runner.bot = "gemma"
    monkeypatch.setattr(runner, "is_running", lambda: True)
    denied = runner.start("claude-a", "new request")
    assert denied["ok"] is False
    assert "requested bot 'claude-a'" in denied["error"]
    assert "conversation 'conv-a'" in denied["error"]
    assert "gemma" in denied["error"]


def test_control_stop_file_honors_the_runner_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace())
    from control import surfaces
    default = tmp_path / "default-stop.json"
    scoped = tmp_path / "conv-a-stop.json"
    monkeypatch.setattr(surfaces, "STOP_FILE", str(default))
    monkeypatch.setenv("OPERATOR_STOP_PATH", str(scoped))
    surfaces.arm_stop()
    assert scoped.exists()
    assert not default.exists()


def test_two_real_runner_process_groups_stop_independently(
        tmp_path, monkeypatch) -> None:
    """Acceptance seam: real AgentRunner threads and real child process groups,
    without spending a model turn. Stopping one conversation must not touch the
    other, and both terminal transitions remain attributable for history."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPERATOR_SESSION_ROOT", str(tmp_path / "work"))
    monkeypatch.setattr(OA, "_resolve_claude", lambda: "/fake/claude")
    monkeypatch.setattr(OA, "_resolve_codex", lambda: "/fake/codex")
    monkeypatch.setattr(OA, "_squad_boot_context", lambda _bot: "")
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if cmd and str(cmd[0]).startswith("/fake/"):
            return real_popen(["sleep", "30"], stdout=subprocess.PIPE,
                              text=True, start_new_session=True)
        # stop() also uses subprocess.run(["ps", ...]) to reap detached MCP
        # children. Let diagnostics through; globally replacing them with a
        # sleeper would test the fake rather than process-group isolation.
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(OA.subprocess, "Popen", fake_popen)
    recorded = []
    monkeypatch.setattr(
        OA.operator_history, "record",
        lambda runner, reason="": recorded.append(
            (runner.conversation_id, reason)))
    runners = OA.RunnerRegistry(global_limit=2, per_bot_limit=1)
    assert runners.start("claude-a", "one", conversation_id="conv-a")["ok"]
    assert runners.start("gpt", "two", conversation_id="conv-b")["ok"]
    a, b = runners.get("conv-a"), runners.get("conv-b")
    for _ in range(100):
        if a._proc is not None and b._proc is not None:
            break
        time.sleep(0.02)
    assert a.is_running() and b.is_running()

    assert runners.stop(conversation_id="conv-a")["ok"]
    a._thread.join(5)
    assert not a.is_running()
    assert b.is_running(), "stopping conv-a killed conv-b's process group"
    assert runners.snapshot(conversation_id="conv-b")["bot"] == "gpt"

    assert runners.stop(conversation_id="conv-b")["ok"]
    b._thread.join(5)
    assert not b.is_running()
    assert {cid for cid, _reason in recorded} == {"conv-a", "conv-b"}
