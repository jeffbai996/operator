"""Per-runtime launch adapters for the operator agent (1.0.9 R4).

Each supported runtime (claude / codex / agy) assembles its own argv, folds
the persona/boot-context into the prompt its own way, and owns its MCP-config
side effect (claude: a per-run config file; codex: a static config.toml entry;
agy: the fixed global ~/.gemini mcp_config.json). Everything a builder needs
comes in through RunSpec; everything the runner must know comes back in
LaunchPlan — no AgentRunner state is touched here.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from operator_prompts import AGY_STEPWISE_DIRECTIVE

_BROWSE = os.path.expanduser("~/agents/browse")
_CONTROL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control")


@dataclass(frozen=True)
class RunSpec:
    """Everything a launch needs, snapshot at build time (no runner state)."""
    binpath: str
    bot: str
    task: str           # fully wrapped (transcript inject + nudges + directive)
    persona: str        # demo/surface-aware, already built
    boot_context: str   # the app boot text; '' on resume/demo/claude
    model: str
    effort: str
    surface: str
    demo: bool
    real_ok: bool
    resume_id: str
    config_dir: str


@dataclass
class LaunchPlan:
    cmd: list
    env: dict = field(default_factory=dict)   # env updates to apply pre-spawn
    mcp_config_path: str = ""                 # MCP config this launch wrote
    agy_brain_dir: str = ""                   # agy: trajectory dir to snapshot
    agy_mcp_dir: str = ""                     # agy: config dir the MCP write touched


def _ensure_codex_control_mcp(config_dir: str) -> None:
    """Idempotently wire the operator-control MCP into codex's config.toml
    (driver parity). A static entry is safe: the MCP reads OPERATOR_SURFACE /
    OPERATOR_REAL_OK from the process env it inherits at spawn, so the same
    entry serves every surface. Best-effort; never raises."""
    try:
        path = os.path.join(config_dir, "config.toml")
        if not os.path.exists(path):
            return
        with open(path) as f:
            txt = f.read()
        if "[mcp_servers.operator-control]" in txt:
            return
        with open(path, "a") as f:
            f.write('\n[mcp_servers.operator-control]\ncommand = "bash"\n'
                    'args = ["' + os.path.join(_CONTROL, "operator-mcp.sh") + '"]\n')
    except OSError:
        pass


def build_codex_cmd(spec: RunSpec) -> LaunchPlan:
    """codex exec: headless, JSONL events, ChatGPT-sub token (no API key).
    Its CODEX_HOME config.toml already wires the playwright MCP, so we just
    exec it. `codex exec resume <thread_id>` threads context."""
    env: dict = {}
    # demo: a minimal CODEX_HOME with ONLY the playwright MCP (no owner-tool/
    # plugins) -> browser is the agent's only tool, satisfying the sandbox spec.
    if spec.demo:
        env["CODEX_HOME"] = os.path.expanduser(os.environ.get("OPERATOR_DEMO_CODEX_HOME", "~/.operator-sandbox/codex"))
    else:
        env["CODEX_HOME"] = spec.config_dir
        _ensure_codex_control_mcp(spec.config_dir)   # driver parity
    # codex >= 0.144 surfaces MCP tools through its exec-JS bridge
    # (`tools.mcp__<server>__<tool>`), NOT as first-class function tools.
    # The model's visible function list genuinely lacks `perceive`/`computer`,
    # so on desktop surfaces gpt (esp. at low effort) answered "I don't have
    # those tools" without attempting a call (2026-07-11, verified: 0/5 tool
    # attempts with the persona prompt, 5/6 once told to call the MCP tool).
    # Name the bridge explicitly so the persona's tool list stays true.
    _bridge = ""
    if spec.surface != "browser" and not spec.demo:
        _bridge = (
            "\n\nTOOL ACCESS (codex): `computer`, `perceive` and `game_macro` "
            "are MCP tools from the `operator-control` server. If they are not "
            "in your top-level function list, they are STILL available through "
            "your exec tool's JS bridge — call e.g. "
            "`await tools.mcp__operator_control__perceive({})`, "
            "`await tools.mcp__operator_control__computer({action:'screenshot'})`, "
            "`await tools.mcp__operator_control__game_macro({...})`. "
            "Never reply that these tools are missing without actually "
            "attempting one of those calls first.")
    # PARITY: boot context only on the first turn of a thread (resumes already
    # carry it). Kept full, not compressed: ~92% cached after turn 1, and the
    # shared-memory awareness is worth the one-time cold cost.
    # the bridge note goes right before the task (recency: 20k+ chars of boot
    # context otherwise sit between the note and the ask the model acts on)
    prompt = (spec.persona
              + (("\n\n=== SQUAD CONTEXT (your shared memory + roster) ===\n"
                  + spec.boot_context) if spec.boot_context else "")
              + _bridge
              + "\n\nTask: " + spec.task)
    # DEMO: run codex INSIDE a bwrap FS sandbox (sandbox.sh) — tmpfs over
    # $HOME hides ~/repos, ~/.claude, ~/.codex, the app data; only the empty
    # workspace + auth + browse module are bound. codex's built-in shell/file
    # tools physically cannot reach owner/the app files. Non-demo keeps the
    # bypass (it's the owner's trusted local cockpit). The browser MCP runs
    # as a separate subprocess and still reaches the isolated Chrome.
    if spec.demo:
        _sandbox = os.path.expanduser(os.environ.get("OPERATOR_SANDBOX_SCRIPT", "~/.operator-sandbox/sandbox.sh"))
        cmd = ["bash", _sandbox, spec.binpath, "exec", "--json",
               "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox"]
    else:
        cmd = [spec.binpath, "exec", "--json", "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox"]
    if spec.model:
        cmd += ["-m", spec.model]
    if spec.effort:
        cmd += ["-c", "model_reasoning_effort=" + json.dumps(spec.effort)]
    # Surface tool routing (mirrors the claude adapter): on a DESKTOP surface,
    # disable the browser Playwright MCP so GPT's only screenshot tool is the
    # surface-aware control MCP — otherwise it defaults to
    # browser_take_screenshot and "only sees the browser screen" on desktop
    # runs. Browser surface keeps Playwright. (Demo already uses a
    # playwright-only CODEX_HOME.)
    if not spec.demo and spec.surface != "browser":
        cmd += ["-c", "mcp_servers.playwright.enabled=false"]
    # codex >= 0.144 spawns MCP servers with a SCRUBBED env — the parent's
    # OPERATOR_SURFACE / OPERATOR_REAL_OK / OPERATOR_BOT never reached the
    # control server (verified 2026-07-12 with an env-dump stub server), so it
    # silently defaulted to the browser surface: perceive watched the WRONG
    # SCREEN on sandbox runs and _tools() withheld the `computer` tool — gpt
    # had eyes on the browser and no hands at all. Pass the run context
    # explicitly via the per-server env config (dotted -c override).
    if not spec.demo and spec.surface == "browser":
        # loud-failure contract (see build_claude_cmd): codex scrubs the env
        # it hands MCP servers, so it must ride the per-server env config.
        _pw = "mcp_servers.playwright.env."
        cmd += ["-c", _pw + 'OPERATOR_REQUIRE_CDP="1"']
    if not spec.demo:
        _ctl = "mcp_servers.operator-control.env."
        cmd += ["-c", _ctl + 'OPERATOR_SURFACE="' + spec.surface + '"']
        if spec.bot:
            cmd += ["-c", _ctl + 'OPERATOR_BOT="' + spec.bot + '"']
        if spec.real_ok:
            cmd += ["-c", _ctl + 'OPERATOR_REAL_OK="1"']
    if spec.resume_id:
        cmd += ["resume", spec.resume_id, prompt]
    else:
        cmd += [prompt]
    return LaunchPlan(cmd=cmd, env=env)


def build_agy_cmd(spec: RunSpec) -> LaunchPlan:
    """agy (Google Antigravity CLI): headless `-p` PRINT mode returns PLAIN
    TEXT — the final answer only, no JSON event stream (the live trace is
    reverse-engineered from the trajectory on disk; see operator_agent's agy
    hooks). agy reads its MCP servers from the FIXED ~/.gemini config path —
    there is no per-run --mcp-config flag — so we wire the playwright server
    in there idempotently and non-destructively (preserve other servers)."""
    env = {"GEMINI_CLI_CONFIG_DIR": spec.config_dir}  # informational; agy uses ~/.gemini
    if not spec.demo:
        # loud-failure contract (see build_claude_cmd): agy spawns stdio MCPs
        # with an inherited env, so the process-level var reaches the launcher.
        env["OPERATOR_REQUIRE_CDP"] = "1"
    mcp_path = os.path.join(spec.config_dir, "config", "mcp_config.json")
    agy_mcp_dir = ""
    try:
        os.makedirs(os.path.dirname(mcp_path), exist_ok=True)
        existing = {}
        try:
            with open(mcp_path) as f:
                _raw = f.read().strip()
            if _raw:
                existing = json.loads(_raw)
                if not isinstance(existing, dict):
                    existing = {}
        except (OSError, ValueError):
            existing = {}
        servers = existing.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        # add/overwrite ONLY our entries; leave everything else as-is.
        servers["playwright"] = {"command": "bash",
            "args": [os.path.join(_BROWSE, "playwright-mcp.sh"), spec.bot]}
        # driver parity: gemma gets the control MCP (computer/perceive/
        # game_macro) whenever a run actually drives a desktop surface. The
        # demo IS allowed the control MCP  — its sandbox
        # surface routes every action through `docker exec` into the isolated
        # container, so the agent drives the container, never the host.
        if spec.surface != "browser":
            servers["operator-control"] = {"command": "bash",
                "args": [os.path.join(_CONTROL, "operator-mcp.sh")]}
        else:
            # browser run: make sure a PRIOR desktop run's leftover control
            # MCP is gone (teardown no longer strips — 2026-06-29), so the
            # config always matches THIS run's surface and a plain gemma
            # session never inherits desktop tools.
            servers.pop("operator-control", None)
        existing["mcpServers"] = servers
        tmp = mcp_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(existing, f, indent=2)
        os.replace(tmp, mcp_path)
        agy_mcp_dir = spec.config_dir
    except OSError:
        pass
    # agy has no --append-system-prompt (a claude flag) — FOLD persona +
    # the app self-context + task into the -p prompt (like the codex adapter),
    # plus the agy-only stepwise directive (Flash one-shots its whole plan
    # otherwise and the live trace lands in a burst instead of streaming).
    prompt = (spec.persona
              + (("\n\n=== SQUAD CONTEXT (your shared memory + roster) ===\n"
                  + spec.boot_context) if spec.boot_context else "")
              + "\n\n" + AGY_STEPWISE_DIRECTIVE + "Task: " + spec.task)
    # --dangerously-skip-permissions = agy analog of codex's bypass-approvals
    # (auto-approve tool/MCP calls non-interactively). Resume: agy -p emits
    # no session id, but the id IS the new .db in its conversations dir —
    # captured by set-difference after the run, threaded back via
    # --conversation on the next turn.
    cmd = [spec.binpath, "-p", prompt, "--dangerously-skip-permissions"]
    if spec.resume_id:
        cmd += ["--conversation", spec.resume_id]
    if spec.model:
        cmd += ["--model", spec.model]
    return LaunchPlan(
        cmd=cmd, env=env, mcp_config_path=mcp_path, agy_mcp_dir=agy_mcp_dir,
        agy_brain_dir=os.path.join(spec.config_dir, "antigravity-cli", "brain"))


def build_claude_cmd(spec: RunSpec) -> LaunchPlan:
    """claude -p: stream-json, Max/Pro OAuth in the bot's config dir, MCP
    servers via a per-run config file this adapter writes."""
    cfg_path = os.path.join(os.path.expanduser("~/.cache/computer-use"),
                            f"operator-mcp-{spec.bot}.json")
    # Tool routing by surface: browser keeps Playwright (+ the control MCP
    # for perceive/game_macro); desktop surfaces get ONLY the control MCP —
    # a browser tool on a desktop run would mislead the model. Demo keeps
    # the original playwright-only config (the control MCP has local-
    # perception file access the public sandbox must not inherit).
    _op_entry = {"command": "bash",
                 "args": [os.path.join(_CONTROL, "operator-mcp.sh")],
                 "env": {"OPERATOR_SURFACE": spec.surface,
                         "OPERATOR_BOT": spec.bot,
                         **({"OPERATOR_REAL_OK": "1"} if spec.real_ok else {})}}
    _pw_entry = {"command": "bash",
                 "args": [os.path.join(_BROWSE, "playwright-mcp.sh"), spec.bot]}
    if not spec.demo:
        # REQUIRE_CDP kills the silent headless fallback for cockpit runs: an
        # unreachable Chrome must fail the MCP loudly, not browse invisibly in
        # a window the live feed never shows. Demo keeps OPERATOR_DEMO_CDP.
        _pw_entry["env"] = {"OPERATOR_REQUIRE_CDP": "1"}
    if spec.demo:
        servers = {"playwright": _pw_entry}
    elif spec.surface == "browser":
        servers = {"playwright": _pw_entry, "operator-control": _op_entry}
    else:
        servers = {"operator-control": _op_entry}
    try:
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        with open(cfg_path, "w") as f:
            json.dump({"mcpServers": servers}, f)
    except OSError:
        pass
    cmd = [spec.binpath, "-p", spec.task,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions",
           # --settings/--strict-mcp-config both BREAK --resume (verified).
           "--mcp-config", cfg_path,
           "--append-system-prompt", spec.persona]
    if spec.resume_id:
        cmd += ["--resume", spec.resume_id]
    if spec.model:
        cmd += ["--model", spec.model]
    if spec.effort:
        cmd += ["--effort", spec.effort]
    return LaunchPlan(cmd=cmd, env={"CLAUDE_CONFIG_DIR": spec.config_dir},
                      mcp_config_path=cfg_path)


_ADAPTERS = {"codex": build_codex_cmd, "agy": build_agy_cmd,
             "claude": build_claude_cmd}


def build_cmd(runtime: str, spec: RunSpec) -> LaunchPlan:
    """Dispatch to the runtime's adapter. Unknown runtime = a config bug —
    raise (KeyError) rather than guess a launch command."""
    return _ADAPTERS[runtime](spec)
