"""1.0.9 R4 — per-runtime launch adapters: exact argv + MCP-config ownership.

Each runtime's command assembly (and its MCP-config side effect) lives in
operator_runtimes; these tests pin the exact argv shape per runtime so the
_run_inner decomposition can't silently drop a flag. The claude path's full
byte parity is additionally covered by test_operator_prompts (fixture replay
through the real launch path).

Run from modules/operator:  PYTHONPATH=. pytest tests/test_operator_runtimes.py -q
"""
import json
import re
import shutil
import subprocess
import os
import time

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


def test_background_browser_policy_survives_scrubbed_runtime_environments():
    spec = _spec(background_browser=True, conversation_id='job')
    codex = RT.build_cmd('codex', spec)
    assert 'mcp_servers.playwright.env.OPERATOR_BACKGROUND_BROWSER="1"' in codex.cmd
    claude = RT.build_cmd('claude', spec)
    servers = json.load(open(claude.mcp_config_path))['mcpServers']
    assert servers['playwright']['env']['OPERATOR_BACKGROUND_BROWSER'] == '1'
    agy = RT.build_cmd('agy', spec)
    assert agy.env['OPERATOR_BACKGROUND_BROWSER'] == '1'


def test_shared_connectors_reach_all_runtime_adapters(fake_home):
    profile = fake_home / '.gemini' / 'config'
    profile.mkdir(parents=True)
    (profile / 'mcp_config.json').write_text(json.dumps({'mcpServers': {
        'lookup': {'serverUrl': 'https://example.com/mcp'},
        'notes': {'command': 'notes-server', 'args': ['--stdio']},
        'off': {'command': 'off-server', 'disabled': True}}}))
    claude = RT.build_cmd('claude', _spec())
    servers = json.load(open(claude.mcp_config_path))['mcpServers']
    assert servers['lookup'] == {'type': 'http', 'url': 'https://example.com/mcp'}
    assert servers['notes']['command'] == 'notes-server'
    agy = RT.build_cmd('agy', _spec(config_dir=str(fake_home / '.gemini')))
    servers = json.load(open(agy.mcp_config_path))['mcpServers']
    assert servers['lookup'] == {'serverUrl': 'https://example.com/mcp'}
    codex = RT.build_cmd('codex', _spec(config_dir=str(fake_home / '.codex-operator')))
    assert 'mcp_servers."lookup".url="https://example.com/mcp"' in codex.cmd
    assert 'mcp_servers."notes".command="notes-server"' in codex.cmd
    assert not any('off-server' in arg for arg in codex.cmd)


@pytest.mark.parametrize('tier', ['default', 'priority', 'ultrafast'])
def test_codex_speed_override_survives_native_transport_conversion(tier):
    from operator_codex_transport import command
    plan = RT.build_cmd('codex', _spec(service_tier=tier))
    assert f'service_tier="{tier}"' in plan.cmd
    assert f'service_tier="{tier}"' in command(plan)


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


def test_claude_run_uses_the_config_dirs_long_lived_token(fake_home):
    config_dir = fake_home / ".claude"
    config_dir.mkdir()
    (config_dir / "oauth-token").write_text("  token-value\n")
    plan = RT.build_cmd("claude", _spec(config_dir=str(config_dir)))
    assert plan.env["CLAUDE_CODE_OAUTH_TOKEN"] == "token-value"


@pytest.mark.parametrize("contents", [None, "", " \n"])
def test_claude_run_without_a_token_file_keeps_stored_oauth(fake_home, contents):
    config_dir = fake_home / ".claude"
    config_dir.mkdir()
    if contents is not None:
        (config_dir / "oauth-token").write_text(contents)
    plan = RT.build_cmd("claude", _spec(config_dir=str(config_dir)))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in plan.env


def test_claude_resume_model_effort_flags():
    plan = RT.build_cmd("claude", _spec(resume_id="sess-1",
                                        model="claude-sonnet-5-5", effort="medium"))
    c = plan.cmd
    assert c[c.index("--resume") + 1] == "sess-1"
    assert c[c.index("--model") + 1] == "claude-sonnet-5-5"
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
    plan = RT.build_cmd("codex", _spec(model="gpt-6.1-sol", effort="low"))
    c = plan.cmd
    assert c[c.index("-m") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="low"' in c


def test_codex_operator_disables_invisible_web_browsing() -> None:
    """The cockpit streams the Playwright Chrome. Codex's separate web-search
    extension can research invisibly while that Chrome sits untouched, so an
    Operator run must not receive that second browser."""
    plan = RT.build_cmd("codex", _spec(surface="browser"))
    assert 'web_search="disabled"' in plan.cmd
    # Browser feature flags gate both built-in and external browser families.
    # Leave them alone so the Playwright bridge remains exposed.
    assert "browser_use" not in plan.cmd
    assert "browser_use_external" not in plan.cmd


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


def test_codex_cockpit_uses_release_local_playwright_launcher():
    """Operator must not execute the mutable main-checkout launcher from the
    user's config.toml. A dirty mode bit there previously removed Playwright
    from every Codex run without a visible startup error."""
    plan = RT.build_cmd("codex", _spec())
    assert 'mcp_servers.playwright.command="bash"' in plan.cmd
    args_override = next(
        value for value in plan.cmd
        if value.startswith("mcp_servers.playwright.args="))
    args = json.loads(args_override.split("=", 1)[1])
    expected = os.path.abspath(os.path.join(
        os.path.dirname(RT.__file__), "..", "browse", "playwright-mcp.sh"))
    assert args == [expected]


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


def test_agy_run_retires_legacy_controls_and_preserves_shared_connectors(fake_home):
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
    assert set(isolated) == {"playwright", "user-server"}
    assert isolated['user-server'] == original['mcpServers']['user-server']
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


def test_agy_workspace_browser_run_has_only_operator_owned_mcps(fake_home):
    plan = RT.build_cmd("agy", _spec(
        config_dir=os.path.expanduser("~/.gemini"),
        run_id="run-123", run_credential="test-credential",
        workspace_dir=str(fake_home / "workspace")))

    servers = json.load(open(_agy_plan_cfg(plan)))["mcpServers"]
    assert set(servers) == {"playwright", "operator-control"}


def test_agy_cockpit_pins_the_operator_chrome(fake_home):
    """agy pins the cockpit while keeping MCP child home paths functional."""
    plan = RT.build_cmd("agy", _spec(config_dir=os.path.expanduser("~/.gemini")))
    assert plan.env["BROWSE_CHROME_PORT"] == "9222"
    assert plan.env["OPERATOR_REQUIRE_CDP"] == "1"
    servers = json.load(open(_agy_plan_cfg(plan)))["mcpServers"]
    assert servers["playwright"]["env"]["HOME"] == str(fake_home)


def test_agy_resume_and_model_flags():
    plan = RT.build_cmd("agy", _spec(resume_id="conv-3", model="gemini-3.8-flash",
                                     config_dir=os.path.expanduser("~/.gemini")))
    c = plan.cmd
    assert c[c.index("--conversation") + 1] == "conv-3"
    assert c[c.index("--model") + 1] == "gemini-3.8-flash"


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


def test_personal_app_codex_disables_implicit_workspace_and_memory_context():
    plan = RT.build_cmd('codex', _spec(personal_app=True, bot='gpt', config_dir=os.path.expanduser('~/.codex-operator')))
    assert 'project_doc_max_bytes=0' in plan.cmd
    assert 'developer_instructions=""' in plan.cmd
    assert 'features.memories=false' in plan.cmd


@pytest.mark.parametrize('surface', ['browser', 'desktop-sandbox', 'desktop-real'])
@pytest.mark.parametrize('demo', [False, True])
def test_direct_claude_harness_marks_operator_context(surface, demo):
    plan = RT.build_claude_cmd(_spec(surface=surface, demo=demo))
    assert plan.env['OPERATOR_SURFACE'] == surface


@pytest.mark.parametrize('resume_id', ['', 'sess-1'])
@pytest.mark.parametrize('surface', ['browser', 'desktop-sandbox', 'desktop-real'])
def test_claude_startup_wait_is_bounded_without_disabling_integrations(monkeypatch, resume_id, surface):
    monkeypatch.delenv('CLAUDE_CODE_MCP_STARTUP_WAIT_MS', raising=False)
    plan = RT.build_claude_cmd(_spec(resume_id=resume_id, surface=surface))
    assert plan.env['CLAUDE_CODE_MCP_STARTUP_WAIT_MS'] == '2000'
    assert '--strict-mcp-config' not in plan.cmd
    assert '--settings' not in plan.cmd
    assert ('--resume' in plan.cmd) == bool(resume_id)
    monkeypatch.setenv('CLAUDE_CODE_MCP_STARTUP_WAIT_MS', '5000')
    override = RT.build_claude_cmd(_spec(resume_id=resume_id, surface=surface))
    assert override.env['CLAUDE_CODE_MCP_STARTUP_WAIT_MS'] == '5000'


def _credentials(config_dir, **oauth):
    config_dir.mkdir(exist_ok=True)
    (config_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))


def test_claude_auth_is_usable_with_a_long_lived_token(fake_home):
    config_dir = fake_home / ".claude"
    _credentials(config_dir, accessToken="", refreshToken="", expiresAt=0)
    (config_dir / "oauth-token").write_text("token-value\n")
    assert RT.claude_auth_problem(str(config_dir), "claude-a") is None


def test_claude_auth_is_usable_with_a_live_refresh_token(fake_home):
    config_dir = fake_home / ".claude"
    future_ms = (time.time() + 86400) * 1000
    _credentials(config_dir, accessToken="a", refreshToken="r", expiresAt=0,
                 refreshTokenExpiresAt=future_ms)
    assert RT.claude_auth_problem(str(config_dir), "claude-a") is None


@pytest.mark.parametrize("oauth", [
    {"accessToken": "", "refreshToken": "", "expiresAt": 0},
    {"accessToken": "a", "refreshToken": "r", "expiresAt": 0,
     "refreshTokenExpiresAt": 1_000_000},
])
def test_claude_auth_refuses_a_wiped_or_lapsed_login(fake_home, oauth):
    config_dir = fake_home / ".claude"
    _credentials(config_dir, **oauth)
    problem = RT.claude_auth_problem(str(config_dir), "claude-a")
    assert problem.startswith("Claude sign-in for claude-a has expired.")


def test_claude_auth_does_not_guess_without_a_credentials_file(fake_home):
    # macOS keeps the login in the Keychain, not in the config folder.
    assert RT.claude_auth_problem(str(fake_home / ".claude"), "claude-a") is None


def test_claude_auth_trusts_an_inherited_token(fake_home, monkeypatch):
    config_dir = fake_home / ".claude"
    _credentials(config_dir, accessToken="", refreshToken="", expiresAt=0)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "inherited")
    assert RT.claude_auth_problem(str(config_dir), "claude-a") is None


# ── agy browser tool signatures (the owner 2026-10-19) ───────────────────────────
# Argon spent a model turn per new tool reading its schema before calling it.
# The agy browser prompt now carries the core Playwright signatures; a wrong
# signature would be worse than none, so they are checked against the
# installed @playwright/mcp tool list.

_SIG_LINE = re.compile(r"^- (browser_\w+)\((.*)\)", re.M)


def _agy_browser_prompt(**over):
    plan = RT.build_cmd("agy", _spec(config_dir=os.path.expanduser("~/.gemini"), **over))
    return plan.cmd[2]


def test_agy_browser_prompt_carries_core_tool_signatures():
    prompt = _agy_browser_prompt()
    names = {name for name, _ in _SIG_LINE.findall(prompt)}
    assert {"browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_take_screenshot"} <= names
    assert "schema" in prompt.lower()          # tells the model no lookup is needed


def test_agy_desktop_prompt_has_no_browser_tool_signatures():
    prompt = _agy_browser_prompt(surface="desktop-sandbox")
    assert not _SIG_LINE.search(prompt)


def _installed_playwright_tools() -> dict:
    """tools/list from the installed @playwright/mcp, or skip when absent."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "..", "..", "browse"),
                  os.path.expanduser("~/agents/browse"),
                  "/data/dev/repos/agents-repo/modules/browse"]
    browse = next((c for c in candidates
                   if os.path.exists(os.path.join(c, "node_modules/@playwright/mcp/cli.js"))), None)
    if not browse or not shutil.which("node"):
        pytest.skip("@playwright/mcp is not installed on this host")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "sig-check", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.Popen(
        ["node", "node_modules/@playwright/mcp/cli.js", "--caps", "vision,pdf",
         "--headless", "--isolated"],
        cwd=browse, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True)
    try:
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in msgs))
        proc.stdin.flush()
        deadline = time.time() + 20
        while time.time() < deadline:
            line = proc.stdout.readline()
            if '"id":2' in line:
                return {t["name"]: t["inputSchema"]
                        for t in json.loads(line)["result"]["tools"]}
        pytest.fail("@playwright/mcp did not answer tools/list")
    finally:
        proc.kill()
        proc.wait()


def test_agy_tool_signatures_match_installed_playwright_mcp():
    tools = _installed_playwright_tools()
    sigs = _SIG_LINE.findall(RT.AGY_BROWSER_TOOLS)
    assert sigs
    for name, params in sigs:
        assert name in tools, f"{name} is not a Playwright MCP tool"
        props = tools[name].get("properties", {})
        required = set(tools[name].get("required", []))
        listed = {}
        for p in filter(None, (s.strip() for s in params.split(","))):
            key = re.match(r"(\w+)(\??)", p)
            listed[key.group(1)] = key.group(2) == "?"
        for key, optional in listed.items():
            assert key in props, f"{name}: no parameter {key!r}"
            # a defaulted parameter may be shown either way
            if "default" not in props[key]:
                assert optional == (key not in required), \
                    f"{name}: {key!r} optionality is wrong"
        missing = {r for r in required if "default" not in props[r]} - set(listed)
        assert not missing, f"{name}: required {missing} not listed"


_JOB_SIG_LINE = re.compile(r"^- (job_\w+)\((.*)\)", re.M)


def test_agy_job_run_prompt_carries_job_result_signature():
    prompt = _agy_browser_prompt(run_id="run-123", run_credential="test-credential",
                                 workspace_dir=os.path.expanduser("~/workspace"))
    assert "job_result" in {n for n, _ in _JOB_SIG_LINE.findall(prompt)}


def test_agy_prompt_without_job_tools_has_no_job_signature():
    assert not _JOB_SIG_LINE.search(_agy_browser_prompt())
    assert not _JOB_SIG_LINE.search(_agy_browser_prompt(
        demo=True, run_id="run-123", run_credential="test-credential"))


def test_agy_job_signatures_match_the_job_tool_schemas():
    import operator_job_tools
    tools = {t["name"]: t["inputSchema"] for t in operator_job_tools.TOOLS}
    sigs = _JOB_SIG_LINE.findall(RT.AGY_JOB_TOOLS)
    assert sigs
    for name, params in sigs:
        props = tools[name]["properties"]
        required = set(tools[name].get("required", []))
        listed = {}
        for p in filter(None, (s.strip() for s in params.split(","))):
            key = re.match(r"(\w+)(\??)", p)
            listed[key.group(1)] = key.group(2) == "?"
        assert set(listed) == set(props), f"{name}: parameters differ"
        for key, optional in listed.items():
            assert optional == (key not in required), f"{name}: {key!r} optionality"


def test_agy_browser_prompt_says_to_read_the_linked_snapshot():
    # Playwright 0.0.76 links each action's page snapshot as a file. Calling
    # browser_snapshot again costs a second step for the same page.
    low = _agy_browser_prompt().lower()
    assert "snapshot file" in low and "browser_snapshot again" in low
