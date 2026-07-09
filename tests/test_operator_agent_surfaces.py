"""Track C tests — AgentRunner surface gating: validation is SERVER-side (the
UI confirm is a courtesy), demo can never leave the browser, every runtime may
drive desktop surfaces (driver parity), stop() arms the control-layer kill switch.

Run from the repo root:  PYTHONPATH=. pytest tests/test_operator_agent_surfaces.py -q
"""
import json
import os

import pytest

import operator_agent as OA


@pytest.fixture
def runner(monkeypatch, tmp_path):
    # state file + kill switch land under a throwaway HOME
    monkeypatch.setenv("HOME", str(tmp_path))
    # stub the runtime-binary lookups so the suite is hermetic — start() rejects
    # with "<runtime> binary not found" when the real CLI isn't on PATH (true in
    # clean CI), which is a launch precondition, not what these tests exercise.
    monkeypatch.setattr(OA, "_resolve_claude", lambda: "/fake/claude")
    monkeypatch.setattr(OA, "_resolve_codex", lambda: "/fake/codex")
    monkeypatch.setattr(OA, "_resolve_agy", lambda: "/fake/agy")
    r = OA.AgentRunner()
    r._run = lambda binpath, b, task: None      # never launch a real agent
    return r


def test_unknown_surface_rejected(runner):
    r = runner.start("claude-a", "t", surface="hologram")
    assert not r["ok"] and "hologram" in r["error"]


def test_desktop_real_requires_real_ok(runner):
    r = runner.start("claude-a", "t", surface="desktop-real")
    assert not r["ok"] and "confirmation" in r["error"]


def test_desktop_real_with_confirm_starts(runner):
    r = runner.start("claude-a", "t", surface="desktop-real", real_ok=True)
    assert r["ok"]
    assert runner.surface == "desktop-real" and runner._real_ok is True


def test_desktop_surface_open_to_all_runtimes(runner):
    # driver parity (2026-07-08): desktop dispatch is no longer claude-only —
    # codex/agy get the operator-control MCP too. (Binary resolution is
    # host-dependent, so assert the claude-gate is gone, not a clean start.)
    r = runner.start("gpt", "t", surface="desktop-sandbox")
    assert "claude" not in (r.get("error") or "")
    if r["ok"]:
        assert runner.surface == "desktop-sandbox"


def test_demo_forces_browser(runner):
    r = runner.start("claude-a", "t", surface="desktop-sandbox", demo=True)
    assert r["ok"]
    assert runner.surface == "browser"


def test_browser_default_and_snapshot_carries_surface(runner):
    r = runner.start("claude-a", "do a thing")
    assert r["ok"]
    assert runner.snapshot()["surface"] == "browser"


class _FakeProc:
    returncode = 0
    stdout = iter(())

    def poll(self):
        return 0

    def wait(self):
        return 0


def test_desktop_launch_path_builds_without_raising(monkeypatch, tmp_path):
    """Regression: the pre-spawn section (desktop persona swap, MCP config)
    once died on a .format() KeyError — the mandate text contains literal
    braces — leaving state='running' with no process and no error surfaced.
    Drive _run for real up to a stubbed Popen and demand a clean finish."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # hermetic: don't depend on the real claude CLI being on PATH (absent in CI)
    monkeypatch.setattr(OA, "_resolve_claude", lambda: "/fake/claude")
    r = OA.AgentRunner()
    launched = {}

    def fake_popen(cmd, **kw):
        launched["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(OA.subprocess, "Popen", fake_popen)
    res = r.start("claude-a", "screenshot the desktop", surface="desktop-sandbox")
    assert res["ok"]
    r._thread.join(timeout=10)
    assert launched, "Popen was never reached — launch died pre-spawn"
    assert r.state == "done", (r.state, r.messages[-3:])
    # the desktop persona actually swapped in (flavor text present in cmd)
    joined = " ".join(str(c) for c in launched["cmd"])
    assert "ISOLATED Linux desktop" in joined
    assert "{surface_flavor}" not in joined


def test_stop_arms_kill_switch(runner, tmp_path):
    runner.stop()
    stop_file = tmp_path / ".cache" / "computer-use" / "operator-stop.json"
    assert stop_file.exists()
    assert json.loads(stop_file.read_text())["ts"] > 0
