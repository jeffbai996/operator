"""1.0.8 R2 — the prompt extraction must not change a single byte.

tests/fixtures/prompt_snapshots.json was captured through the REAL launch
path (fake Popen recording argv) BEFORE the extraction. These tests replay
the same cases through today's code and demand byte-identical output — the
directive/persona a model sees is a contract, and silent drift here changes
agent behavior without any test noticing.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_operator_prompts.py -q
"""
import json
import os

import pytest

import operator_agent as OA
import operator_prompts as P

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "prompt_snapshots.json")


def _cases():
    with open(FIXTURE) as f:
        return json.load(f)


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPERATOR_COMPLETION_GATE", "0")
    monkeypatch.setattr(OA, "_resolve_claude", lambda: "/fake/claude")
    return OA.AgentRunner()


class _FakeProc:
    def __init__(self):
        self.stdout = iter(())
        self.returncode = 0
        self.pid = 999999

    def wait(self):
        return 0

    def poll(self):
        return 0


@pytest.mark.parametrize("entry", _cases(),
                         ids=lambda e: f"{e['case']['surface']}-demo{e['case']['demo']}-{len(e['case']['task'])}")
def test_launch_path_prompt_bytes_match_pre_refactor(runner, monkeypatch, entry):
    captured = []
    monkeypatch.setattr(OA.subprocess, "Popen",
                        lambda cmd, **kw: (captured.append(cmd), _FakeProc())[1])
    c = entry["case"]
    res = runner.start(c["bot"], c["task"], demo=c["demo"],
                       surface=c["surface"], real_ok=c["real_ok"])
    assert res["ok"], res
    runner._thread.join(timeout=15)
    assert captured, "launch never reached Popen"
    cmd = captured[0]
    assert cmd[cmd.index("-p") + 1] == entry["task_arg"]
    assert cmd[cmd.index("--append-system-prompt") + 1] == entry["persona"]


# ── direct builder behavior (cheaper to reason about than full snapshots) ────

def test_chatty_task_passes_through_unwrapped():
    assert P.wrap_task("hi", "browser", False) == "hi"


def test_browser_wrap_prepends_directive_and_keeps_task_last():
    out = P.wrap_task("Find the cheapest flight", "browser", False)
    assert out.startswith("SYSTEM DIRECTIVE")
    assert out.endswith("USER REQUEST: Find the cheapest flight")


def test_desktop_wrap_names_the_surface():
    out = P.wrap_task("open a terminal", "desktop-sandbox", False)
    assert "surface: desktop-sandbox" in out and "LIVE DESKTOP" in out


def test_persona_desktop_swap_has_no_unfilled_placeholder():
    p = P.build_persona("You are X." + P.BROWSER_MANDATE, "desktop-sandbox", False)
    assert "{surface_flavor}" not in p
    assert "ISOLATED Linux desktop" in p
    assert "You are X." in p


def test_demo_persona_strips_squad_identity():
    p = P.build_persona("You are claude-a." + P.BROWSER_MANDATE, "browser", True)
    assert "claude-a" not in p
