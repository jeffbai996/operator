"""Track C tests — AgentRunner surface gating: validation is SERVER-side (the
UI confirm is a courtesy), demo can never leave the browser, every runtime may
drive desktop surfaces (driver parity), stop() arms the control-layer kill switch.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_operator_agent_surfaces.py -q
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


def test_demo_allows_isolated_sandbox(runner):
    # #27: the demo may drive the ISOLATED sandbox container (host services
    # are localhost-bound, so the docker bridge gateway leads nowhere).
    r = runner.start("claude-a", "t", surface="desktop-sandbox", demo=True)
    assert r["ok"]
    assert runner.surface == "desktop-sandbox"


def test_demo_still_coerces_desktop_real_to_browser(runner):
    # even WITH the confirm flag a public demo can never touch the real machine
    r = runner.start("claude-a", "t", surface="desktop-real", real_ok=True, demo=True)
    assert r["ok"]
    assert runner.surface == "browser"
    assert runner._real_ok is False


def test_demo_sandbox_gets_desktop_mandate_without_squad_identity(runner):
    import operator_agent as OA
    runner.demo = True
    runner.surface = "desktop-sandbox"
    p = runner._persona_for_run(OA.AGENT_BOTS["claude-a"])
    assert "LIVE COMPUTER DESKTOP" in p and "ISOLATED Linux desktop" in p
    assert "claude-a" not in p and "{surface_flavor}" not in p


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


def _codex_cmd_for_surface(monkeypatch, tmp_path, surface):
    """Drive the codex launch path to a stubbed Popen and return the built cmd."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(OA, "_resolve_codex", lambda: "/fake/codex")
    # boot context hits the host-app / network — stub it out for hermeticity
    monkeypatch.setattr(OA, "_squad_boot_context", lambda bot="gpt": "")
    r = OA.AgentRunner()
    launched = {}

    def fake_popen(cmd, **kw):
        launched["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(OA.subprocess, "Popen", fake_popen)
    res = r.start("gpt", "screenshot the desktop", surface=surface, real_ok=True)
    assert res["ok"], res
    r._thread.join(timeout=10)
    assert launched, "Popen was never reached — codex launch died pre-spawn"
    return launched["cmd"]


def test_codex_desktop_disables_playwright_mcp(monkeypatch, tmp_path):
    """The bug: on a desktop surface, codex kept the browser Playwright MCP and
    GPT called browser_take_screenshot → "only sees the browser screen". Fix
    passes -c mcp_servers.playwright.enabled=false so its only screenshot tool
    is the surface-aware control MCP (mirrors the claude path's server routing)."""
    cmd = _codex_cmd_for_surface(monkeypatch, tmp_path, "desktop-sandbox")
    assert "mcp_servers.playwright.enabled=false" in cmd


def test_codex_desktop_real_disables_playwright_mcp(monkeypatch, tmp_path):
    cmd = _codex_cmd_for_surface(monkeypatch, tmp_path, "desktop-real")
    assert "mcp_servers.playwright.enabled=false" in cmd


def test_codex_browser_keeps_playwright_mcp(monkeypatch, tmp_path):
    """Browser surface must NOT disable Playwright — it's the whole toolset there."""
    cmd = _codex_cmd_for_surface(monkeypatch, tmp_path, "browser")
    assert "mcp_servers.playwright.enabled=false" not in cmd


def test_stop_arms_kill_switch(runner, tmp_path):
    runner.stop()
    stop_file = tmp_path / ".cache" / "computer-use" / "operator-stop.json"
    assert stop_file.exists()
    assert json.loads(stop_file.read_text())["ts"] > 0
