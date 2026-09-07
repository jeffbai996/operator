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
    # default-instance baseline: no instance CDP override leaking in from the
    # host env (operator-fam sets OPERATOR_DEMO_CDP on its server process)
    monkeypatch.delenv("OPERATOR_DEMO_CDP", raising=False)
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


def test_claude_cockpit_pins_the_operator_chrome(fake_home):
    """The bot-Chrome split flipped the shared launcher's default to :9224;
    a cockpit run must pin the Windows browser the feed streams (:9222)."""
    plan = RT.build_cmd("claude", _spec())
    servers = json.load(open(plan.mcp_config_path))["mcpServers"]
    assert servers["playwright"]["env"]["BROWSE_CHROME_PORT"] == "9222"
    assert servers["playwright"]["env"]["OPERATOR_REQUIRE_CDP"] == "1"


def test_conversation_scope_reaches_runtime_and_mcp_children(fake_home):
    scoped = _spec(conversation_id="conv-a", stop_path="/tmp/conv-a-stop")
    claude = RT.build_cmd("claude", scoped)
    servers = json.load(open(claude.mcp_config_path))["mcpServers"]
    assert claude.env["OPERATOR_CONVERSATION_ID"] == "conv-a"
    assert servers["operator-control"]["env"]["OPERATOR_STOP_PATH"] == "/tmp/conv-a-stop"
    assert servers["playwright"]["env"]["OPERATOR_CONVERSATION_ID"] == "conv-a"

    codex = RT.build_cmd("codex", scoped)
    assert 'mcp_servers.operator-control.env.OPERATOR_CONVERSATION_ID="conv-a"' in codex.cmd
    assert 'mcp_servers.playwright.env.OPERATOR_CONVERSATION_ID="conv-a"' in codex.cmd

    agy = RT.build_cmd("agy", scoped)
    assert agy.env["OPERATOR_CONVERSATION_ID"] == "conv-a"
    assert agy.env["OPERATOR_STOP_PATH"] == "/tmp/conv-a-stop"


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
    assert "operator-demo/codex" in plan.env["CODEX_HOME"]
    assert "mcp_servers.playwright.enabled=false" not in plan.cmd
    assert not any("BROWSE_CHROME_PORT" in a for a in plan.cmd)   # demo: no pin


def test_codex_cockpit_pins_the_operator_chrome():
    """codex scrubs the env it hands MCP servers, so the :9222 pin must ride
    the per-server config overrides (see test_claude_cockpit_pins...)."""
    plan = RT.build_cmd("codex", _spec())
    assert 'mcp_servers.playwright.env.BROWSE_CHROME_PORT="9222"' in plan.cmd
    assert 'mcp_servers.playwright.env.OPERATOR_REQUIRE_CDP="1"' in plan.cmd


# ── agy ──────────────────────────────────────────────────────────────────────

def _agy_cfg(tmp):
    return os.path.join(str(tmp), ".gemini", "config", "mcp_config.json")


def _agy_plan_cfg(plan):
    return os.path.join(plan.env["HOME"], ".gemini", "config", "mcp_config.json")


def test_agy_argv_and_isolated_mcp_write(fake_home):
    plan = RT.build_cmd("agy", _spec(config_dir=os.path.expanduser("~/.gemini")))
    assert plan.cmd[0] == "/fake/bin" and plan.cmd[1] == "-p"
    assert "--dangerously-skip-permissions" in plan.cmd
    prompt = plan.cmd[2]
    assert prompt.startswith("PERSONA") and prompt.endswith("Task: TASK TEXT")
    assert "ONE STEP AT A TIME" in prompt          # agy stepwise directive folded in
    assert plan.env["HOME"] != str(fake_home)
    servers = json.load(open(_agy_plan_cfg(plan)))["mcpServers"]
    assert "playwright" in servers
    assert "operator-control" not in servers       # browser run wires no desktop tools
    assert plan.agy_brain_dir.endswith("antigravity-cli/brain")
    assert plan.mcp_config_path == _agy_plan_cfg(plan)
    assert not os.path.exists(_agy_cfg(fake_home))


def test_agy_desktop_surface_wires_control_mcp(fake_home):
    plan = RT.build_cmd("agy", _spec(surface="desktop-sandbox",
                                     config_dir=os.path.expanduser("~/.gemini")))
    servers = json.load(open(_agy_plan_cfg(plan)))["mcpServers"]
    assert set(servers) == {"playwright", "operator-control"}


def test_agy_run_retires_legacy_operator_mcps_and_excludes_global_servers(fake_home):
    cfg = _agy_cfg(fake_home)
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    original = {"mcpServers": {
        "playwright": {"command": "bash", "args": ["/old/browse/playwright-mcp.sh", "gemma"]},
        "operator-control": {"command": "bash", "args": ["/old/control/operator-mcp.sh"]},
        "user-server": {"command": "keep-me"},
    }, "unrelatedSetting": True}
    with open(cfg, "w") as f:
        json.dump(original, f)
    cache = fake_home / ".gemini" / "antigravity-cli" / "mcp"
    (cache / "playwright").mkdir(parents=True)
    (cache / "operator-control").mkdir()
    (cache / "user-server").mkdir()

    plan = RT.build_cmd("agy", _spec(config_dir=os.path.expanduser("~/.gemini")))

    isolated = json.load(open(_agy_plan_cfg(plan)))["mcpServers"]
    assert set(isolated) == {"playwright"}
    assert json.load(open(cfg)) == {
        "mcpServers": {"user-server": {"command": "keep-me"}},
        "unrelatedSetting": True,
    }
    assert not (cache / "playwright").exists()
    assert not (cache / "operator-control").exists()
    assert (cache / "user-server").is_dir()


def test_agy_legacy_retirement_preserves_user_owned_same_name_servers(fake_home):
    cfg = _agy_cfg(fake_home)
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    original = {"mcpServers": {
        "playwright": {"command": "custom-playwright"},
        "operator-control": {"command": "python3", "args": ["control/server.py"]},
    }}
    with open(cfg, "w") as f:
        json.dump(original, f)

    RT.build_cmd("agy", _spec(config_dir=os.path.expanduser("~/.gemini")))

    assert json.load(open(cfg)) == original


def test_agy_isolated_home_preserves_user_home_and_antigravity_state(fake_home):
    marker = fake_home / "ordinary-user-file"
    marker.write_text("available")
    state = fake_home / ".gemini" / "antigravity-cli"
    conversations = state / "conversations"
    brain = state / "brain"
    conversations.mkdir(parents=True)
    brain.mkdir()
    (state / "antigravity-oauth-token").write_text("test-token")
    (conversations / "conv-3.db").write_text("resume-state")

    plan = RT.build_cmd("agy", _spec(resume_id="conv-3",
                                     config_dir=os.path.expanduser("~/.gemini")))

    run_home = plan.env["HOME"]
    assert open(os.path.join(run_home, "ordinary-user-file")).read() == "available"
    run_state = os.path.join(run_home, ".gemini", "antigravity-cli")
    assert os.path.samefile(os.path.join(run_state, "conversations"), conversations)
    assert os.path.samefile(os.path.join(run_state, "brain"), brain)
    assert os.path.samefile(os.path.join(run_state, "antigravity-oauth-token"),
                            state / "antigravity-oauth-token")
    assert not os.path.islink(os.path.join(run_state, "mcp"))
    assert plan.agy_brain_dir == str(brain)

    run_token = os.path.join(run_state, "antigravity-oauth-token")
    os.unlink(run_token)
    with open(run_token, "w") as f:
        f.write("refreshed-test-token")
    RT.cleanup_launch_plan(plan)
    assert not os.path.exists(run_home)
    assert (conversations / "conv-3.db").read_text() == "resume-state"
    assert (state / "antigravity-oauth-token").read_text() == "refreshed-test-token"


def test_concurrent_agy_plans_have_independent_surface_configs(fake_home):
    browser = RT.build_cmd("agy", _spec(config_dir=os.path.expanduser("~/.gemini")))
    desktop = RT.build_cmd("agy", _spec(surface="desktop-real", real_ok=True,
                                         config_dir=os.path.expanduser("~/.gemini")))

    assert browser.env["HOME"] != desktop.env["HOME"]
    browser_servers = json.load(open(_agy_plan_cfg(browser)))["mcpServers"]
    desktop_servers = json.load(open(_agy_plan_cfg(desktop)))["mcpServers"]
    assert set(browser_servers) == {"playwright"}
    assert set(desktop_servers) == {"playwright", "operator-control"}
    assert not os.path.exists(_agy_cfg(fake_home))


def test_agy_cockpit_pins_the_operator_chrome(fake_home):
    """agy pins the cockpit while keeping MCP child home paths functional."""
    plan = RT.build_cmd("agy", _spec(config_dir=os.path.expanduser("~/.gemini")))
    assert plan.env["BROWSE_CHROME_PORT"] == "9222"
    assert plan.env["OPERATOR_REQUIRE_CDP"] == "1"
    servers = json.load(open(_agy_plan_cfg(plan)))["mcpServers"]
    assert servers["playwright"]["env"]["HOME"] == str(fake_home)


def test_agy_resume_and_model_flags():
    plan = RT.build_cmd("agy", _spec(resume_id="conv-3", model="gemini-3.7-flash",
                                     config_dir=os.path.expanduser("~/.gemini")))
    c = plan.cmd
    assert c[c.index("--conversation") + 1] == "conv-3"
    assert c[c.index("--model") + 1] == "gemini-3.7-flash"


def test_unknown_runtime_raises():
    with pytest.raises(KeyError):
        RT.build_cmd("mystery", _spec())


# ── second full-function instance (operator-fam) — instance CDP override ───────

_OP2_CDP = "http://127.0.0.1:9333"


def test_claude_cockpit_honors_instance_cdp_override(fake_home, monkeypatch):
    """operator-fam runs full-function against its OWN Chrome: the server's
    OPERATOR_DEMO_CDP (the historical explicit-endpoint name) must replace the
    default :9222 pin explicitly."""
    monkeypatch.setenv("OPERATOR_DEMO_CDP", _OP2_CDP)
    plan = RT.build_cmd("claude", _spec())
    servers = json.load(open(plan.mcp_config_path))["mcpServers"]
    env = servers["playwright"]["env"]
    assert env["OPERATOR_DEMO_CDP"] == _OP2_CDP
    assert env["OPERATOR_REQUIRE_CDP"] == "1"
    assert "BROWSE_CHROME_PORT" not in env


def test_codex_cockpit_honors_instance_cdp_override(monkeypatch):
    monkeypatch.setenv("OPERATOR_DEMO_CDP", _OP2_CDP)
    plan = RT.build_cmd("codex", _spec())
    joined = " ".join(plan.cmd[:-1])
    assert 'mcp_servers.playwright.env.OPERATOR_DEMO_CDP="' + _OP2_CDP + '"' in joined
    assert 'mcp_servers.playwright.env.OPERATOR_REQUIRE_CDP="1"' in joined
    assert "BROWSE_CHROME_PORT" not in joined


def test_agy_cockpit_honors_instance_cdp_override(fake_home, monkeypatch):
    monkeypatch.setenv("OPERATOR_DEMO_CDP", _OP2_CDP)
    plan = RT.build_cmd("agy", _spec(config_dir=os.path.expanduser("~/.gemini")))
    assert plan.env["OPERATOR_DEMO_CDP"] == _OP2_CDP
    assert plan.env["OPERATOR_REQUIRE_CDP"] == "1"
    assert "BROWSE_CHROME_PORT" not in plan.env
