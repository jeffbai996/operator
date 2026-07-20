"""1.0.9 R4 — per-runtime launch adapters: exact argv + MCP-config ownership.

Each runtime's command assembly (and its MCP-config side effect) lives in
operator_runtimes; these tests pin the exact argv shape per runtime so the
_run_inner decomposition can't silently drop a flag. The claude path's full
byte parity is additionally covered by test_operator_prompts (fixture replay
through the real launch path).

Run from modules/operator:  PYTHONPATH=. pytest tests/test_operator_runtimes.py -q
"""
import json
import os

import pytest

import operator_runtimes as RT


@pytest.fixture(autouse=True)
def fake_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _spec(**over):
    base = dict(binpath="/fake/bin", bot="claude-a", task="TASK TEXT",
                persona="PERSONA", boot_context="", model="", effort="",
                surface="browser", demo=False, real_ok=False, resume_id="",
                config_dir=os.path.expanduser("~/.claude"))
    base.update(over)
    return RT.RunSpec(**base)


# ── claude ───────────────────────────────────────────────────────────────────

def test_claude_argv_and_mcp_config(fake_home):
    plan = RT.build_cmd("claude", _spec())
    cfg = os.path.expanduser("~/.cache/computer-use/operator-mcp-claude-a.json")
    assert plan.cmd == ["/fake/bin", "-p", "TASK TEXT",
                        "--output-format", "stream-json", "--verbose",
                        "--permission-mode", "bypassPermissions",
                        "--mcp-config", cfg,
                        "--append-system-prompt", "PERSONA"]
    assert plan.mcp_config_path == cfg
    servers = json.load(open(cfg))["mcpServers"]
    assert set(servers) == {"playwright", "operator-control"}   # browser surface
    assert plan.env["CLAUDE_CONFIG_DIR"] == os.path.expanduser("~/.claude")


def test_claude_resume_model_effort_flags():
    plan = RT.build_cmd("claude", _spec(resume_id="sess-1",
                                        model="claude-sonnet-5", effort="medium"))
    c = plan.cmd
    assert c[c.index("--resume") + 1] == "sess-1"
    assert c[c.index("--model") + 1] == "claude-sonnet-5"
    assert c[c.index("--effort") + 1] == "medium"


def test_claude_desktop_surface_gets_control_mcp_only(fake_home):
    plan = RT.build_cmd("claude", _spec(surface="desktop-sandbox"))
    servers = json.load(open(plan.mcp_config_path))["mcpServers"]
    assert set(servers) == {"operator-control"}     # no browser tool on desktop
    assert servers["operator-control"]["env"]["OPERATOR_SURFACE"] == "desktop-sandbox"


def test_claude_desktop_real_confirm_reaches_mcp_env(fake_home):
    plan = RT.build_cmd("claude", _spec(surface="desktop-real", real_ok=True))
    servers = json.load(open(plan.mcp_config_path))["mcpServers"]
    assert servers["operator-control"]["env"]["OPERATOR_REAL_OK"] == "1"


def test_claude_demo_is_playwright_only(fake_home):
    plan = RT.build_cmd("claude", _spec(demo=True))
    servers = json.load(open(plan.mcp_config_path))["mcpServers"]
    assert set(servers) == {"playwright"}   # control MCP never reaches the demo
    assert "env" not in servers["playwright"]   # demo attaches via OPERATOR_DEMO_CDP


def test_claude_cockpit_requires_the_visible_chrome(fake_home):
    """A cockpit run refuses the invisible headless fallback: if the feed's
    Chrome is unreachable, the MCP fails loudly instead (2026-07-20)."""
    plan = RT.build_cmd("claude", _spec())
    servers = json.load(open(plan.mcp_config_path))["mcpServers"]
    assert servers["playwright"]["env"]["OPERATOR_REQUIRE_CDP"] == "1"


# ── codex ────────────────────────────────────────────────────────────────────

def test_codex_argv_prompt_folds_persona_and_task():
    plan = RT.build_cmd("codex", _spec())
    assert plan.cmd[:5] == ["/fake/bin", "exec", "--json", "--skip-git-repo-check",
                            "--dangerously-bypass-approvals-and-sandbox"]
    prompt = plan.cmd[-1]
    assert prompt.startswith("PERSONA") and prompt.endswith("Task: TASK TEXT")
    assert plan.env["CODEX_HOME"] == os.path.expanduser("~/.claude")


def test_codex_boot_context_folds_in_on_cold_start():
    plan = RT.build_cmd("codex", _spec(boot_context="SQUAD BOOT"))
    assert "SQUAD CONTEXT" in plan.cmd[-1] and "SQUAD BOOT" in plan.cmd[-1]


def test_codex_resume_threads_the_conversation():
    plan = RT.build_cmd("codex", _spec(resume_id="thread-9"))
    c = plan.cmd
    assert c[c.index("resume") + 1] == "thread-9"
    assert c[-1].endswith("Task: TASK TEXT")     # prompt still last


def test_codex_model_and_effort_flags():
    plan = RT.build_cmd("codex", _spec(model="gpt-5.6-sol", effort="low"))
    c = plan.cmd
    assert c[c.index("-m") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="low"' in c


def test_codex_desktop_surface_disables_playwright():
    plan = RT.build_cmd("codex", _spec(surface="desktop-sandbox"))
    assert "mcp_servers.playwright.enabled=false" in plan.cmd


def test_codex_demo_wraps_in_sandbox_and_isolated_home():
    plan = RT.build_cmd("codex", _spec(demo=True))
    assert plan.cmd[0] == "bash" and plan.cmd[1].endswith("sandbox.sh")
    assert "operator-sandbox/codex" in plan.env["CODEX_HOME"]
    assert "mcp_servers.playwright.enabled=false" not in plan.cmd
    assert not any("BROWSE_CHROME_PORT" in a for a in plan.cmd)   # demo: no pin


def test_codex_cockpit_requires_the_visible_chrome():
    """codex scrubs the env it hands MCP servers, so the contract must ride
    the per-server config overrides."""
    plan = RT.build_cmd("codex", _spec())
    assert 'mcp_servers.playwright.env.OPERATOR_REQUIRE_CDP="1"' in plan.cmd


# ── agy ──────────────────────────────────────────────────────────────────────

def _agy_cfg(tmp):
    return os.path.join(str(tmp), ".gemini", "config", "mcp_config.json")


def test_agy_argv_and_global_mcp_write(fake_home):
    plan = RT.build_cmd("agy", _spec(config_dir=os.path.expanduser("~/.gemini")))
    assert plan.cmd[0] == "/fake/bin" and plan.cmd[1] == "-p"
    assert "--dangerously-skip-permissions" in plan.cmd
    prompt = plan.cmd[2]
    assert prompt.startswith("PERSONA") and prompt.endswith("Task: TASK TEXT")
    assert "ONE STEP AT A TIME" in prompt          # agy stepwise directive folded in
    servers = json.load(open(_agy_cfg(fake_home)))["mcpServers"]
    assert "playwright" in servers
    assert "operator-control" not in servers       # browser run wires no desktop tools
    assert plan.agy_brain_dir.endswith("antigravity-cli/brain")
    assert plan.mcp_config_path == _agy_cfg(fake_home)


def test_agy_desktop_surface_wires_control_mcp(fake_home):
    plan = RT.build_cmd("agy", _spec(surface="desktop-sandbox",
                                     config_dir=os.path.expanduser("~/.gemini")))
    servers = json.load(open(_agy_cfg(fake_home)))["mcpServers"]
    assert "operator-control" in servers


def test_agy_browser_run_strips_stale_control_entry(fake_home):
    cfg = _agy_cfg(fake_home)
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    json.dump({"mcpServers": {"operator-control": {"command": "x"},
                              "user-server": {"command": "keep-me"}}}, open(cfg, "w"))
    RT.build_cmd("agy", _spec(config_dir=os.path.expanduser("~/.gemini")))
    servers = json.load(open(cfg))["mcpServers"]
    assert "operator-control" not in servers   # prior desktop run's leftover gone
    assert servers["user-server"]["command"] == "keep-me"   # others preserved


def test_agy_cockpit_requires_the_visible_chrome(fake_home):
    """agy inherits its process env into stdio MCPs, so the contract rides
    plan.env — and stays OUT of the shared ~/.gemini config."""
    plan = RT.build_cmd("agy", _spec(config_dir=os.path.expanduser("~/.gemini")))
    assert plan.env["OPERATOR_REQUIRE_CDP"] == "1"
    servers = json.load(open(_agy_cfg(fake_home)))["mcpServers"]
    assert "env" not in servers["playwright"]


def test_agy_resume_and_model_flags():
    plan = RT.build_cmd("agy", _spec(resume_id="conv-3", model="Gemini 3.5 Flash (High)",
                                     config_dir=os.path.expanduser("~/.gemini")))
    c = plan.cmd
    assert c[c.index("--conversation") + 1] == "conv-3"
    assert c[c.index("--model") + 1] == "Gemini 3.5 Flash (High)"


def test_unknown_runtime_raises():
    with pytest.raises(KeyError):
        RT.build_cmd("mystery", _spec())
