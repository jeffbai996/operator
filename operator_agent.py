"""operator_agent.py — run a headless Claude Code agent that drives the browser.

Option 1 : the operator IS the agent. We spawn `claude -p` in a
background thread, as the chosen persona, with the Playwright MCP pointed at the
SAME logged-in Chrome the operator views — authenticated on the Max SUBSCRIPTION
(claude reads ~/.claude/.credentials.json), zero metered API spend. We parse its
stream-json output live: assistant text → the operator chat, browser tool calls
→ the action trail. No Discord, no live-session dependency, no spam.

Only the host personas that can drive: claude-a + claude-b.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time

_log = logging.getLogger("operator.agent")

# Prompt prose (personas, mandates, SYSTEM DIRECTIVEs, gate prompts) lives
# in operator_prompts (1.0.8 R2); aliased for AGENT_BOTS and the agy path.
import operator_agy
import operator_history
import operator_runtimes
import operator_steer
import operator_prompts as _prompts
from operator_prompts import (
    AGY_STEPWISE_DIRECTIVE as _AGY_STEPWISE_DIRECTIVE,
    BROWSER_MANDATE as _BROWSER_MANDATE,
    GEMMA_SELF as _GEMMA_SELF,
    GPT_SELF as _GPT_SELF,
)

# Squad self-context for gpt. The Claude bots get this from their own CLAUDE.md +
# a SessionStart hook that loads the shared host-app; codex has neither, so gpt
# was running with no idea who/what it is. Keep this short — it's prepended every turn.
def _squad_boot_context(bot: str = "gpt") -> str:
    """Slim the app context for Operator runs (browser tasks don't need the full digest).

    Loads:
    - SQUAD.md rulebook (behavioral rules, ~5.7k tokens)
    - SYSTEM.md roster + endpoints (~731 tokens)
    - Feedback memories — full bodies (behavioral rules must be pre-loaded)
    - Memory INDEX only for everything else (names + tags, ~2k tokens)
    - Instruction to use host-app recall / search for deeper lookup

    The full digest (format_store_digest) loads ~18.9k tokens of memory bodies that
    a browser-task agent rarely needs. Index + search covers it at ~1/10th the cost.
    Fail-soft: if host-app isn't importable, gpt/gemma runs without it."""
    try:
        import sys as _sys
        _ss = os.path.expanduser("~/.host-app")
        if _ss not in _sys.path:
            _sys.path.insert(0, _ss)
        import store as _store  # type: ignore
        parts = []
        for fn in ("format_squad_doc_for_prompt", "format_system_doc_for_prompt"):
            try:
                v = getattr(_store, fn)(bot=bot)
                if v:
                    parts.append(v)
            except Exception:
                pass
        # Feedback memories: full bodies (behavioral rules must be pre-loaded)
        try:
            fb = _store.format_memories_for_prompt(bot=bot, types=["feedback"])
            if fb:
                parts.append(fb)
        except Exception:
            pass
        # All other memories: index only (name + tags + one-liner description)
        try:
            idx = _store.format_memories_index(bot=bot, exclude_types=["feedback"])
            if idx:
                parts.append(idx)
        except Exception:
            pass
        # Journal (recent) + files index
        try:
            jou = _store.format_journal_for_prompt(days=3)
            if jou:
                parts.append(jou)
        except Exception:
            pass
        try:
            fi = _store.format_files_index(bot=bot)
            if fi:
                parts.append(fi)
        except Exception:
            pass
        parts.append(
            "MEMORY ACCESS: The above is an index. To read a specific memory's full "
            "text: `host-app memory show <id>`. To search by topic: "
            "`host-app recall \"<query>\"` (semantic) or search MCP tool if available."
        )
        return "\n\n".join(parts)
    except Exception:
        return ""


# personas that can drive + the config dir whose stored sub-creds + identity they
# run under. (Both ride the default ~/.claude credentials = the Max login.)
AGENT_BOTS = {
    "claude-a": {"label": "claude-a", "runtime": "claude",
               "config_dir": os.path.expanduser("~/.claude"),
               "cwd": os.path.expanduser("~/.operator-sessions/claude-a"),
               "persona": "You are a helpful, capable computer-using assistant." + _BROWSER_MANDATE},
    "claude-b": {"label": "claude-b", "runtime": "claude",
              "config_dir": os.path.expanduser("~/.config/claude-b"),
              "cwd": os.path.expanduser("~/.operator-sessions/claude-b"),
              "persona": "You are a helpful, capable computer-using assistant." + _BROWSER_MANDATE},
    # gpt-bot drives via codex (ChatGPT-sub token, NOT an API key). Its
    # ~/.codex-operator/config.toml wires playwright (Operator-only home); the
    # Unlike the Claude bots, codex has no CLAUDE.md / SessionStart hook loading
    # host-app, so we hand gpt its the app self-context inline via _GPT_SELF.
    "gpt": {"label": "gpt", "runtime": "codex",
            "config_dir": os.path.expanduser("~/.codex-operator"),  # Operator-only CODEX_HOME: has playwright; the interactive gpt Discord bot uses ~/.codex (no playwright) — clean platform separation
            "cwd": os.path.expanduser("~/.operator-sessions/gpt"),
            "persona": ("You are a helpful, capable computer-using assistant." + _GPT_SELF + _BROWSER_MANDATE)},
    # gemma drives via agy (Google Antigravity CLI) on the owner flat Google sub —
    # the agy analog of the codex/ChatGPT-sub path. agy `-p` returns PLAIN TEXT
    # (no JSON event stream), so the live action-trace is unavailable; we surface
    # the final text only. Like gpt/codex, agy has no CLAUDE.md / SessionStart
    # hook, so gemma gets its the app self-context inline (host-app digest if
    # reachable, else _GEMMA_SELF).
    "gemma": {"label": "gemma", "runtime": "agy",
              "config_dir": os.path.expanduser("~/.gemini"),
              "cwd": os.path.expanduser("~/.operator-sessions/gemma"),
              "persona": ("You are a helpful, capable computer-using assistant." + _GEMMA_SELF + _BROWSER_MANDATE)},
}

# Operator's headless agent runs use dedicated cwds (above) so their sessions don't
# clutter the user's interactive `claude --resume`. Make sure the dirs exist.
for _b in AGENT_BOTS.values():
    try: os.makedirs(_b["cwd"], exist_ok=True)
    except Exception: pass


_STEER_HOOK_CMD = ("python3 " + os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "steer_hook.py"))


def _ensure_steer_hook_settings(cwd: str) -> None:
    """Idempotently wire the steer hook (1.0.12) into a bot cwd's PROJECT
    settings (.claude/settings.json). The project FILE is resume-safe — the
    --settings FLAG is what breaks --resume (verified 2026-07-11). Merge-
    preserving: existing keys/hooks survive; re-running with the hook already
    present writes nothing. Best-effort — a failed write only costs mid-loop
    steering (the exit-seam still delivers)."""
    try:
        sp = os.path.join(cwd, ".claude", "settings.json")
        cfg: dict = {}
        try:
            with open(sp, encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                cfg = {}
        except (OSError, ValueError):
            pass
        hooks = cfg.setdefault("hooks", {})
        groups = hooks.setdefault("PostToolUse", [])
        if any(_STEER_HOOK_CMD == h.get("command")
               for g in groups if isinstance(g, dict)
               for h in (g.get("hooks") or []) if isinstance(h, dict)):
            return   # already wired — no churn
        groups.append({"matcher": "",
                       "hooks": [{"type": "command", "command": _STEER_HOOK_CMD}]})
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        tmp = sp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, sp)
    except Exception:  # noqa: BLE001 — never let hook wiring block module import
        pass


for _b in AGENT_BOTS.values():
    if _b.get("runtime") == "claude":   # only claude reads .claude/settings.json
        _ensure_steer_hook_settings(_b["cwd"])



# the surface axis (Track C): what screen the agent drives. Browser = today's
# behavior (Playwright on the logged-in Chrome). Desktop surfaces swap the tool
# set to the operator-control MCP. desktop-real is gated: never default, needs
# an explicit per-session confirm (real_ok), blocked in demo.
SURFACES = ("browser", "desktop-sandbox", "desktop-real")


# Trace labeling + final-text cleaning live in operator_trace (1.0.8 R1);
# imported under the old private names so the runner call sites stay stable.
from operator_trace import (
    action_label as _action_label,
    clean_gemma_text as _clean_gemma_text,
    extract_handoff as _extract_handoff,
    gerund_label as _gerund_label,
    mcp_resource_label as _mcp_resource_label,
)


def _env_int(name: str, default: int) -> int:
    """Env-tunable non-negative int; unset/garbage falls back to the default."""
    try:
        return max(0, int(os.environ.get(name, "")))
    except (TypeError, ValueError):
        return default


def _tok_caps() -> tuple[int, int]:
    """Governor hard caps (#34 phase A): (per-turn, per-run) input-token stops.
    Read from env at call time so caps are live-tunable without a server
    restart; garbage or unset falls back to defaults, 0 disables that cap."""
    return (_env_int("OPERATOR_TOKEN_TURN_STOP", 3_000_000),
            _env_int("OPERATOR_TOKEN_RUN_STOP", 20_000_000))


def _stall_budgets() -> tuple[int, int]:
    """Stall watchdog budgets (v1.1 §2.1): (soft, hard) seconds without ANY
    progress (no new output line / trace step / state change) on a run whose
    process is still alive. Soft → surface `stalled` in the status payload;
    hard → auto-stop like a human Stop tap. Read from env at call time
    (live-tunable, no restart); 0 disables that tier. The defaults are
    deliberately generous — a long single reasoning step is silent-but-working
    and must never trip this (that's why `alive` alone was not enough)."""
    return (_env_int("OPERATOR_STALL_SOFT", 120),
            _env_int("OPERATOR_STALL_HARD", 300))


def _resolve_codex() -> str | None:
    from shutil import which
    c = which("codex")
    if c:
        return c
    nvm = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm):
        for v in sorted(os.listdir(nvm), reverse=True):
            cand = os.path.join(
                nvm, v, "lib", "node_modules", "@openai", "codex",
                "node_modules", "@openai", "codex-linux-x64", "vendor",
                "x86_64-unknown-linux-musl", "bin", "codex")
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
            cand2 = os.path.join(nvm, v, "bin", "codex")
            if os.path.isfile(cand2) and os.access(cand2, os.X_OK):
                return cand2
    return None


def _resolve_claude() -> str | None:
    from shutil import which
    c = which("claude")
    if c:
        return c
    for base in (os.path.expanduser("~/.local/bin/claude"),):
        if os.path.isfile(base) and os.access(base, os.X_OK):
            return base
    nvm = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm):
        for v in sorted(os.listdir(nvm), reverse=True):
            cand = os.path.join(nvm, v, "bin", "claude")
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None


def _resolve_agy() -> str | None:
    """Google Antigravity CLI (`agy`). Drives the browser on the owner flat Google
    sub (no metered API key) — the agy analog of the codex/ChatGPT-sub path."""
    from shutil import which
    a = which("agy")
    if a:
        return a
    base = os.path.expanduser("~/.local/bin/agy")
    if os.path.isfile(base) and os.access(base, os.X_OK):
        return base
    return None





class AgentRunner:
    """Runs ONE headless agent task at a time; streams its output into buffers
    the operator endpoints read. Thread-safe single-flight."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self.bot: str | None = None
        self.task: str | None = None
        self.state: str = "idle"          # idle | running | done | error
        self.surface: str = "browser"     # what screen the agent drives (Track C)
        self._real_ok: bool = False       # per-session desktop-real confirmation
        self.messages: list = []          # [{ts, role, text}] reasoning/replies
        self.started_ts: float = 0.0
        self.model: str = ''
        self.effort: str = ''
        self.ended_ts: float = 0.0
        self.handoff: dict | None = None  # {reason, ts} when the agent asks the human to take over (#4)
        self._cur_session: str = ''       # session id captured this run
        self._agy_buf: list = []          # agy plain-text stdout lines (no JSON stream)
        self._agy_brain_dir: str = ''     # ~/.gemini/antigravity-cli/brain (set per-run)
        self._agy_traj_before: dict = {}  # {trajectory_path: mtime} snapshot pre-launch
        # SHARED conversation transcript across ALL bots (runtime-agnostic) so the
        # convo survives switching claude-a↔claude-b↔gpt. [{role:'user'|'assistant', text}]
        # Persisted to disk so it ALSO survives a server restart (the store/Flask
        # process bounces on every deploy — without this the convo evaporated).
        # DEMO ISOLATION: same-user demo server must never share the real
        # cockpit's transcript/session-id state (launch scripts set the env;
        # the .demo suffix is the backstop — mirrors operator_steer.path()).
        self._state_path = os.environ.get("OPERATOR_STATE_PATH") or (
            os.path.join(os.path.expanduser("~/.cache/computer-use"),
                         "operator-state.json")
            + (".demo" if os.environ.get("OPERATOR_DEMO") else ""))
        self._session_ids: dict = {}      # bot -> last claude session id (resume)
        self._transcript: list = []
        self._last_bot: str | None = None
        # #40b: armed when a run trips the overthink-loop guard; the NEXT agy
        # prompt prepends a loop-break nudge, then clears it (consume-once).
        # Deliberately NOT reset in _run() (that's per-run state) — it must
        # survive from the looping turn to the following one.
        self._agy_loop_nudge_pending: bool = False
        # §2.1: runtime-AGNOSTIC loop guard — armed when a run repeats the same
        # action N× (re-clicking dead coords etc.). Like the agy flag, survives
        # into the NEXT turn (consume-once at prompt build). The counters are
        # per-run (reset in _run_inner); defaults here keep bare parses safe.
        self._repeat_nudge_pending: bool = False
        self._last_action_key: str = ""
        self._action_repeat_streak: int = 0
        self._repeat_warned: bool = False
        self._stall_kill_reason: str = ""
        self._stopped: bool = False
        # B3: a user Stop must survive _run_inner's per-run reset of _stopped —
        # the §3.3 follow-up dispatch gates on THIS flag, which only start()
        # clears. Without it, a Stop landing in the inter-turn gate gap was
        # silently wiped and turn 2 ran to completion anyway.
        self._cancel_requested: bool = False
        # §3.3 completion gate — evidence ledger is per-turn (reset in
        # _run_inner); the fired-flag is per-start() so a user turn gets at
        # most ONE gate/replan follow-up turn, never a loop of them.
        self._consequential_acts: int = 0
        self._acts_since_visual: int = 0
        self._gate_fired: bool = False
        self._gate_pending: bool = False   # true only in the inter-turn gap
        # governor (#34) token accounting — also reset per-run in _run()
        self._peak_in_tokens: int = 0
        self._tok_warned: bool = False
        self._cum_in_tokens: int = 0
        self._tok_stop_fired: bool = False
        # v1.1 §2.2: every state transition goes through _set_state(); this is
        # the progress heartbeat the stall watchdog (§2.1) reads. Bumped on
        # every transition and every consumed output line.
        self.last_progress_ts: float = 0.0
        self._load_state()

    def _set_state(self, new: str, reason: str = "") -> None:
        """SOLE writer of self.state (§2.2). Logs the transition and stamps the
        progress heartbeat so a phantom state can always be traced to a line."""
        old = self.state
        self.state = new
        self.last_progress_ts = time.time()
        if old != new:
            _log.info("operator state %s -> %s%s", old, new,
                      f" ({reason})" if reason else "")
        if old == "running" and new in ("done", "error", "interrupted"):
            # flight recorder (1.0.11): exactly one ledger row per finished
            # run, hooked at the sole state writer so the gate gap's proc-less
            # turns can't double-record. record() never raises by contract.
            try:
                operator_history.record(self, reason=reason)
            except Exception:  # noqa: BLE001 — history must never break a run
                pass

    def _touch(self) -> None:
        """Progress heartbeat: any output line / trace step counts as progress."""
        self.last_progress_ts = time.time()

    def _load_state(self) -> None:
        try:
            with open(self._state_path) as f:
                st = json.load(f)
            self._session_ids = st.get("session_ids", {}) or {}
            self._transcript = st.get("transcript", []) or []
            self._last_bot = st.get("last_bot")
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"session_ids": self._session_ids,
                           "transcript": self._transcript[-40:],
                           "last_bot": self._last_bot}, f)
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def is_running(self) -> bool:
        # state alone lies after a process dies without the _run finally landing
        # (loop crash, killed child, reattach). A "running" state with no live
        # process must read as not-running, or it wedges every future dispatch
        # ("X is already running a task") with nothing actually running.
        if self.state != "running":
            return False
        if self._proc is not None and self._proc.poll() is None:
            return True
        # No live process, state 'running'. Three windows where the run
        # nonetheless legitimately continues: the PRE-SPAWN window (run
        # thread building the prompt/MCP config before Popen lands — plus
        # the start() sliver before the thread exists, covered by
        # _starting), the §3.3 gate gap between turns, and the WRAP-UP
        # sliver after process exit before the terminal state lands.
        # Reading any of these as dead had two real costs: the status
        # poll reported alive:false at birth, which armed the client's
        # dead-run watchdog and killed newborn first turns as a bare
        # "Error" (ledger run #10, 2026-07-11) — and a second dispatch in
        # the pre-spawn window was ACCEPTED, clobbering the spawning run
        # with a concurrent thread. Thread-aliveness is the truth: _run
        # always lands a terminal state before its thread exits, so a
        # dead thread with state still 'running' remains the one genuine
        # wedge, and the unwedge paths still read it as not-running.
        if getattr(self, "_gate_pending", False) or getattr(self, "_starting", False):
            return True
        t = self._thread
        return bool(t is not None and t.is_alive())

    def start(self, bot: str, task: str, model: str = '', effort: str = '',
              demo: bool = False, surface: str = 'browser',
              real_ok: bool = False) -> dict:
        with self._lock:
            if self.is_running():
                return {"ok": False, "error": f"{self.bot} is already running a task"}
            b = AGENT_BOTS.get(bot)
            if not b:
                return {"ok": False, "error": f"'{bot}' can't drive"}
            runtime = b.get("runtime", "claude")
            # ── surface gating (Track C) ───────────────────────────────────
            surface = (surface or "browser").strip()
            if demo and surface == "desktop-real":
                # a public demo can drive the browser or the ISOLATED sandbox
                # container (#27; host services are localhost-bound so the
                # bridge gateway leads nowhere) — but NEVER the real machine.
                surface = "browser"
            if surface not in SURFACES:
                return {"ok": False, "error": f"unknown surface {surface!r}"}
            if surface == "desktop-real" and not real_ok:
                return {"ok": False, "error":
                        "desktop-real needs explicit confirmation (real_ok)"}
            # driver parity (v1.2 roadmap, landed 2026-07-08): every runtime gets
            # the operator-control MCP — codex via a static config.toml entry,
            # agy via the per-run mcpServers write, claude per-run. The MCP reads
            # OPERATOR_SURFACE/OPERATOR_REAL_OK from the inherited process env.
            self.surface = surface
            self._real_ok = bool(real_ok) and surface == "desktop-real"
            if runtime == "codex":
                binpath = _resolve_codex()
                if not binpath:
                    return {"ok": False, "error": "codex binary not found"}
            elif runtime == "agy":
                binpath = _resolve_agy()
                if not binpath:
                    return {"ok": False, "error": "agy binary not found"}
            else:
                binpath = _resolve_claude()
                if not binpath:
                    return {"ok": False, "error": "claude binary not found"}
            self._switched_bot = (self._last_bot is not None and self._last_bot != bot)
            self.bot, self.task = bot, task
            # §2.2: everything from here to _thread.start() runs under a revert
            # guard — an exception in this window used to leave a PHANTOM
            # state='running' with no thread (the class of bug behind the
            # v0.9.0 persona-swap .format() KeyError wedge).
            # _starting spans the set-state → thread.start() sliver so
            # is_running() (and thus the poll's `alive`) never reads the
            # newborn run as dead while _thread is still the previous run's.
            self._starting = True
            self._set_state("running", f"start {bot}")
            operator_steer.clear()   # a fresh run must not inherit stale steers
            try:
                return self._start_locked(bot, task, model, effort, demo, binpath, b)
            except Exception as e:  # noqa: BLE001 — a dead launch must surface
                self._set_state("error", f"pre-spawn failure: {e}")
                self.ended_ts = time.time()
                self.messages.append({"ts": time.time(), "role": "error",
                                      "text": f"launch failed before spawn: {e}"})
                return {"ok": False, "error": f"launch failed: {e}"}
            finally:
                self._starting = False

    def _start_locked(self, bot: str, task: str, model: str, effort: str,
                      demo: bool, binpath: str, b: dict) -> dict:
        """The pre-spawn setup of start() — runs inside its lock + revert guard."""
        self.handoff = None           # fresh run → clear any prior takeover request
        self._cancel_requested = False   # B3: a new run consumes any stale Stop
        self.messages = []
        self._transcript.append({"role": "user", "text": task})
        self._transcript = self._transcript[-40:]
        self._save_state()   # cap
        self.started_ts = time.time()
        self.ended_ts = 0.0
        self._gate_fired = False   # §3.3: one gate/replan follow-up per start()
        self.model, self.effort = (model or '').strip(), (effort or '').strip()
        self.demo = bool(demo)   # demo=True → sandboxed: no the app context/identity
        # default the claude runtime to Sonnet 5 / medium when nothing was picked
        # (empty model would otherwise drop the flag and use the CLI's own default).
        if b.get("runtime") == "claude":
            if not self.model:  self.model = "claude-sonnet-5"
            if not self.effort: self.effort = "medium"
        elif b.get("runtime") == "codex":
            # gpt/codex default: 5.6 Sol / low , matching the UI
            # picker default. Without the effort default, an unset effort drops the
            # -c flag and codex falls back to its config.toml default (xhigh) —
            # needless token burn for browser tasks.
            if not self.model:  self.model = "gpt-5.6-sol"
            if not self.effort: self.effort = "low"
        # agy gets a Gemini display-string default (NOT an API id) — an empty
        # model would otherwise build a broken `--model ""`. effort N/A for agy
        # (it's folded into the model display string, e.g. "(High)").
        elif b.get("runtime") == "agy":
            if not self.model:  self.model = "Gemini 3.5 Flash"
            # agy wants one display string "Gemini X (Tier)" — fold the effort tier in.
            _eff = (self.effort or "high").strip().capitalize()
            if self.model and "(" not in self.model:
                self.model = self.model + " (" + _eff + ")"
            self.effort = ""   # agy has no separate effort flag; it is in the model string
        self._thread = threading.Thread(target=self._run, args=(binpath, b, task),
                                        daemon=True, name="operator-agent")
        self._thread.start()
        return {"ok": True, "bot": bot}

    def _persona_for_run(self, b: dict) -> str:
        """The run's persona — demo/surface-aware; assembly lives in
        operator_prompts.build_persona (1.0.8 R2)."""
        return _prompts.build_persona(b["persona"],
                                      getattr(self, "surface", "browser"),
                                      getattr(self, "demo", False))

    def _run(self, binpath: str, b: dict, task: str) -> None:
        # Everything before the Popen try-block (prompt build, MCP config,
        # persona swap) used to run bare — an exception there killed the thread
        # SILENTLY, leaving state='running' with no process and no error
        # message (the desktop-sandbox .format() KeyError found it, 2026-07-08).
        try:
            followup = self._run_inner(binpath, b, task)
            # Follow-up loop (1.0.12): _run_inner returns a prompt instead of
            # landing a terminal state when the run needs one more resumed
            # turn — the §3.3 gate (bounded to one by _gate_fired) or the
            # steer exit-seam (user-driven, repeats until the queue is dry).
            # B3: gate on _cancel_requested, NOT _stopped — the second
            # _run_inner's per-run reset wipes _stopped, so a Stop landing in
            # the gap raced the reset and lost.
            while followup:
                if self._cancel_requested:
                    # user hit Stop in the gap between the turns — honor it
                    self._gate_pending = False
                    self._set_state("interrupted", "user stop")
                    break
                self.ended_ts = 0.0
                followup = self._run_inner(binpath, b, followup)
        except Exception as e:  # noqa: BLE001 — a dead launch must surface
            self._set_state("error", f"run crashed: {e}")
            self.ended_ts = time.time()
            self.messages.append({"ts": time.time(), "role": "error",
                                  "text": f"launch failed: {e}"})

    def _run_inner(self, binpath: str, b: dict, task: str) -> str | None:
        # Returns a §3.3 gate prompt when the clean exit needs one follow-up
        # verify/replan turn (state stays 'running'); None on every other path.
        self._runtime = b.get("runtime", "claude")
        self._cur_session = ""
        self._agy_buf = []
        self._agy_mcp_dir = ""    # set when we wire agy's global MCP; finally strips it
        self._agy_live_traj = ""  # the run's locked trajectory (live-poll streaming)
        self._peak_in_tokens = 0   # highest single-turn input tokens (context size)
        self._tok_warned = False   # one-shot token-blowout warning per run
        self._cum_in_tokens = 0    # sum of every reported turn-input this run (burn)
        self._tok_stop_fired = False  # one-shot governor cap-stop per run (#34)
        self._agy_traj_before = {}
        self._agy_seen = set()   # step_index already emitted (live-tail dedupe)
        self._agy_noprogress_streak = 0  # consecutive thinking-only planner steps (no
                                          # tool_calls, no content) — the "overthink loop"
                                          # counter 
        self._agy_loop_warned = False    # one-shot stuck-in-a-loop warning per run
        # §2.1 runtime-agnostic repeat-action guard (per-run counters)
        self._last_action_key = ""
        self._action_repeat_streak = 0
        self._repeat_warned = False
        # §3.3 evidence ledger (per-turn): did recent actions include a look?
        self._consequential_acts = 0
        self._acts_since_visual = 0
        self._stall_kill_reason = ""   # set by the stall watchdog before it stops the run
        self._stopped = False    # set by stop(); gates agy interrupt-noise suppression
        # Continuity: inject the shared transcript whenever this turn has NO live
        # native session to resume — i.e. the user switched bots/drivers, OR we
        # have no resume id for this bot (cold start, or the server restarted and
        # wiped the in-memory _session_ids, which would otherwise leave --resume
        # pointing at a dead id and the model starting blind). A live resume id
        # means the native session already holds the history, so skip the inject.
        _have_resume = bool(self._session_ids.get(self.bot or ""))
        _need_inject = (getattr(self, "_switched_bot", False) or not _have_resume)
        if _need_inject and len(self._transcript) > 1:
            convo = "\n".join(
                ("User: " + m["text"]) if m["role"] == "user"
                else ("Assistant: " + m["text"])
                for m in self._transcript[:-1][-12:])   # exclude the current task
            task = ("[Conversation so far, for context — continue it seamlessly:]\n"
                    + convo + "\n\n[Now the user's new request:]\n" + task)
        # consume-once loop nudges (agy think-loop + any-runtime repeat-action)
        task = self._apply_loop_nudge(task)
        # Reinforce browser/desktop-first ON the task text (models weight
        # the prompt heavily, esp. codex/GPT); chatty asks pass unwrapped.
        # The directive prose lives in operator_prompts (1.0.8 R2).
        _surface = getattr(self, "surface", "browser")
        task = _prompts.wrap_task(task, _surface, getattr(self, "demo", False))
        env = dict(os.environ)
        env["OPERATOR_BOT"] = self.bot or ""   # action-tap stamps the right bot
        env["OPERATOR_SURFACE"] = _surface        # control MCP reads the surface
        if getattr(self, "_real_ok", False):
            env["OPERATOR_REAL_OK"] = "1"         # per-session desktop-real confirm
        else:
            env.pop("OPERATOR_REAL_OK", None)
        env["PATH"] = (os.path.expanduser("~/.local/bin") + ":"
                       + os.path.expanduser("~/.nvm/versions/node/v20.20.2/bin")
                       + ":" + env.get("PATH", ""))
        resume_id = self._session_ids.get(self.bot or "")
        # R4 (1.0.9): per-runtime argv + MCP-config assembly lives in
        # operator_runtimes — this method only snapshots state and launches.
        # Squad boot context: claude bots load it via their SessionStart hook;
        # codex/agy have no hook, so their first turn folds it into the prompt
        # (resumes already carry it — don't re-send).
        _boot_bot = {"codex": "gpt", "agy": "gemma"}.get(self._runtime)
        _boot = ("" if (resume_id or getattr(self, "demo", False) or not _boot_bot)
                 else _squad_boot_context(_boot_bot))
        spec = operator_runtimes.RunSpec(
            binpath=binpath, bot=self.bot or "", task=task,
            persona=self._persona_for_run(b), boot_context=_boot,
            model=self.model, effort=self.effort, surface=_surface,
            demo=bool(getattr(self, "demo", False)),
            real_ok=bool(getattr(self, "_real_ok", False)),
            resume_id=resume_id or "", config_dir=b["config_dir"])
        plan = operator_runtimes.build_cmd(self._runtime, spec)
        env.update(plan.env)
        cmd = plan.cmd
        if self._runtime == "agy":
            # runner-owned agy state: where trajectories land + the pre-launch
            # snapshots that identify THIS run's transcript/conversation after
            # the fact (agy -p emits no ids; see the agy hooks below).
            self._agy_mcp_dir = plan.agy_mcp_dir
            self._agy_brain_dir = plan.agy_brain_dir
            self._agy_traj_before = self._agy_snapshot_trajectories()
            self._agy_convs_before = operator_agy.conversation_ids()
        import tempfile as _tf
        _errf = _tf.TemporaryFile(mode="w+", encoding="utf-8")
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=(os.path.expanduser(os.environ.get("OPERATOR_SANDBOX_WORKSPACE", "~/.operator-sandbox/workspace")) if getattr(self,"demo",False) else b["cwd"]), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=_errf, text=True, bufsize=1,
                start_new_session=True)   # own process group → stop() can kill the whole tree (codex + MCP + node + bwrap)
            self._gate_pending = False   # §3.3: the follow-up turn is live now
            self._touch()   # B2: spawn is progress — don't inherit a stale heartbeat
            if self._cancel_requested:
                # a Stop landed in the pre-spawn window, when there was no
                # process to kill — honor it the moment the process exists,
                # instead of letting an invisible run burn to completion.
                # _stopped again (stop()'s own set predates this method's
                # per-run reset above) so _resolve_terminal reads it as an
                # interrupt, not "error exit -15".
                self._stopped = True
                import signal as _sig
                try:
                    os.killpg(os.getpgid(self._proc.pid), _sig.SIGTERM)
                except Exception:  # noqa: BLE001
                    try: self._proc.terminate()
                    except Exception: pass
            if self._runtime == "agy":
                self._start_agy_live_poll()
            for line in self._proc.stdout:
                self._consume(line)
            self._proc.wait()
            if self._runtime == "agy":
                self._flush_agy()   # agy buffers plain text → push as one assistant msg
                # Resume continuity: a resumed run keeps its id; a fresh run's id
                # is the one new .db in the conversations dir. Ambiguous diff
                # (concurrent agy use) → no id, next turn runs fresh.
                if resume_id:
                    self._cur_session = resume_id
                else:
                    _new = operator_agy.new_conversation(
                        getattr(self, "_agy_convs_before", set()),
                        operator_agy.conversation_ids())
                    if _new:
                        self._cur_session = _new
            if self._cur_session and not getattr(self, "_stopped", False):
                self._session_ids[self.bot or ""] = self._cur_session
            elif getattr(self, "_stopped", False):
                # USER STOP: the run was SIGTERM'd mid-stream. Resuming that half-killed
                # session can leave codex hung "Reading additional input from stdin" on the
                # NEXT turn (the stuck-after-interrupt bug). Drop the id so the next turn
                # starts a FRESH session — the shared-transcript inject still carries context.
                self._session_ids.pop(self.bot or "", None)
            # record the final answer in the shared transcript for cross-bot history
            final = next((m["text"] for m in reversed(self.messages)
                          if m.get("role") == "assistant" and m.get("text")), "")
            if final:
                self._transcript.append({"role": "assistant", "text": final[:1500]})
                self._transcript = self._transcript[-40:]
            self._last_bot = self.bot
            self._save_state()
            rc = self._proc.returncode
            if rc == 0:
                gate = self._completion_gate_check()   # §3.3: verify-or-replan
                if gate:
                    # keep is_running() truthful across the proc-less gap;
                    # stamp progress so the stall watchdog doesn't inherit
                    # turn 1's stale heartbeat across the gap (B2)
                    self._gate_pending = True
                    self._touch()
                    return gate    # _run runs one more resumed turn, then done
                steer_fu = self._steer_followup_check()   # 1.0.12 exit seam
                if steer_fu:
                    return steer_fu   # one more resumed turn carrying the steers
            # B4: one deterministic priority decides the terminal label. A stop
            # SIGTERMs the group (non-zero exit — an interrupt, NOT a failure,
            # so no raw kill-signal stderr card), and a stop racing a clean
            # exit 0 must still read as its stop, never "done".
            state, reason = self._resolve_terminal(rc)
            self._set_state(state, reason)
            if state == "error" and reason == f"exit {rc}":
                # a real failure — surface the specific reason from stderr
                # (was discarded before)
                try:
                    _errf.seek(0); _tail = _errf.read().strip()
                    if _tail:
                        # keep the last meaningful lines, capped
                        _msg = "\n".join(_tail.splitlines()[-6:])[:400]
                        if not any(m.get("role") == "error" for m in self.messages[-3:]):
                            self.messages.append({"ts": time.time(), "role": "error",
                                                  "text": f"exit {rc}: {_msg}"})
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001
            self._set_state("error", f"run exception: {e}")
            self.messages.append({"ts": time.time(), "role": "error", "text": str(e)})
        finally:
            try: _errf.close()
            except Exception: pass
            # NOTE (2026-06-29, the owner): playwright now lives PERMANENTLY in ~/.gemini —
            # the anti-archaeology behavioral directive made gemma well-behaved, so she
            # keeps the browser tool and we DON'T strip it on teardown (the per-run
            # write/strip dance was racy + caused gemma's "breaks after 1 step"
            # flakiness). The unused _strip_agy_global_mcp was deleted in 1.0.8.
            self._agy_mcp_dir = ""
            self.ended_ts = time.time()
            self._proc = None

    def _consume(self, line: str) -> None:
        """Parse one stream-json line → push assistant text into messages."""
        self._touch()   # any stdout line = the run is making progress (§2.1)
        if getattr(self, "_runtime", "claude") == "agy":
            # agy `-p` emits PLAIN TEXT (the final answer), not JSON events — there
            # is NO action/tool_use trace on this path (the known agy limitation).
            # Buffer every non-empty stdout line; _flush_agy() (called at process
            # end in _run) joins them into ONE assistant message + runs the
            # handoff extractor so [[TAKE_CONTROL: ...]] still works.
            self._agy_buf.append(line.rstrip("\n"))
            return
        line = line.strip()
        if not line:
            return
        try:
            evt = json.loads(line)
        except Exception:
            return
        # T1: this funnel runs on the run thread with nothing above to catch —
        # a leaked exception kills the reader and wedges the run. Truncated or
        # hostile stream lines can be ANY JSON type ("5", "null" parse fine),
        # and field types are the producer's promise, not a guarantee: guard
        # every shape before touching it, drop what doesn't conform.
        if not isinstance(evt, dict):
            return
        if getattr(self, "_runtime", "claude") == "codex":
            self._consume_codex(evt)
            return
        # capture the session id (for --resume continuity on the next turn);
        # a non-string id would end up inside the next turn's --resume argv
        if evt.get("type") == "system" and evt.get("subtype") == "init":
            sid = evt.get("session_id")
            if isinstance(sid, str) and sid:
                self._cur_session = sid
        # stream-json shape: {type:"assistant", message:{content:[{type:text|tool_use,...}]}}
        if evt.get("type") == "assistant":
            msg = evt.get("message") if isinstance(evt.get("message"), dict) else {}
            # token-blowout guard (claude path): each turn's usage.input_tokens is the
            # context size being re-sent — warn if it balloons (accumulated screenshots).
            _u = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            if _u:
                self._note_token_usage(_u.get("input_tokens"))
            content = msg.get("content")
            for block in (content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    t = block.get("text")
                    t = t.strip() if isinstance(t, str) else ""
                    if t:
                        t, _reason = _extract_handoff(t)
                        if _reason is not None:
                            self.handoff = {"reason": _reason, "ts": time.time()}
                        if t:   # marker may have been the whole message → don't push empty
                            self.messages.append({"ts": time.time(), "role": "assistant", "text": t})
                elif block.get("type") == "tool_use":
                    # surface browser actions inline so the trace interleaves
                    # thinking with actions (Operator-style).
                    name = block.get("name")
                    name = name if isinstance(name, str) else ""
                    args = block.get("input")
                    args = args if isinstance(args, dict) else {}
                    self._note_action(name, args)
                    label, detail = _action_label(name, args)
                    if label:
                        self.messages.append({"ts": time.time(), "role": "action",
                                              "text": label, "detail": detail})
        elif evt.get("type") == "result":
            res = evt.get("result")
            res = res.strip() if isinstance(res, str) else ""
            res, _reason = _extract_handoff(res)
            if _reason is not None and not self.handoff:
                self.handoff = {"reason": _reason, "ts": time.time()}
            # the final assistant turn is usually re-sent as the result — don't
            # append a duplicate (it made the operator show "Worked for Ns" twice).
            last = self.messages[-1]["text"].strip() if self.messages else ""
            if res and res != last:
                self.messages.append({"ts": time.time(), "role": "assistant", "text": res})

    # token-blowout guard: vision-heavy tasks accumulate screenshots that get re-sent
    # every turn, so a single-turn input can balloon into the millions and nuke the
    # subscription rate limit. Watch the per-turn input size; warn ONCE when it crosses
    # a threshold so a runaway is visible in the trace (and the user can stop it).
    _TOKEN_WARN_THRESHOLD = 1_500_000   # single-turn input tokens

    # governor hard caps (#34 phase A) live in module-level _tok_caps() —
    # env-tunable per call (no server restart): OPERATOR_TOKEN_TURN_STOP /
    # OPERATOR_TOKEN_RUN_STOP; 0 disables (back to warn-only).

    # overthink-loop guard threshold — the rule lives with the parser (R5)
    _AGY_LOOP_WARN_STREAK = operator_agy.LOOP_WARN_STREAK

    # §2.1 runtime-agnostic loop guard: N consecutive IDENTICAL actions (same
    # tool + same args — the "clicked, nothing changed, click the same spot
    # again" signature) trips a one-shot trace warning + arms a consume-once
    # nudge for the NEXT turn. Every runtime can loop like this, not just agy;
    # the same pattern kills game grinds (re-clicking a dead target forever).
    # Never auto-kills the run (same policy as the agy guard, the owner 2026-06-30).
    _REPEAT_ACTION_STREAK = 3

    # §3.3 evidence ledger — name fragments that classify a tool call. VISUAL
    # is an explicit look (screenshot/perceive/snapshot). ACT is a consequential
    # action. A playwright browser_* action is consequential AND self-evidencing
    # (its tool result embeds a page snapshot), so it resets the visual counter;
    # desktop `computer` actions carry only the §3.1 changed-verdict, which says
    # "something changed", not "the right thing happened" — they don't.
    _VISUAL_HINTS = ("screenshot", "snapshot", "perceive")
    _ACT_HINTS = ("click", "type", "key", "scroll", "drag", "fill", "navigate",
                  "select", "press", "upload", "drop", "command", "write",
                  "edit", "game_macro", "computer")

    def _note_evidence(self, name: str, args) -> None:
        nl = (name or "").lower()
        act = str(args.get("action", "")).lower() if isinstance(args, dict) else ""
        visual = (any(h in nl for h in self._VISUAL_HINTS) or act == "screenshot")
        if not visual and any(h in nl for h in self._ACT_HINTS):
            self._consequential_acts += 1
            if "browser_" in nl:
                self._acts_since_visual = 0   # playwright result = page state
            else:
                self._acts_since_visual += 1
        elif visual:
            self._acts_since_visual = 0

    def _note_action(self, name: str, args) -> None:
        # T1: callers parse `name` out of external streams/trajectories — a
        # non-str here used to TypeError in BOTH key builds below (the except
        # path re-raised) and kill the reader thread.
        if not isinstance(name, str):
            name = str(name)
        self._note_evidence(name, args)
        try:
            key = name + "|" + json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            key = name + "|" + repr(args)
        if key != self._last_action_key:
            self._last_action_key = key
            self._action_repeat_streak = 1
            return
        self._action_repeat_streak += 1
        if (self._action_repeat_streak >= self._REPEAT_ACTION_STREAK
                and not self._repeat_warned):
            self._repeat_warned = True
            self._repeat_nudge_pending = True   # consume-once, next turn
            self.messages.append({"ts": time.time(), "role": "error",
                "text": ("⚠️ Same action repeated %d× (%s) — this looks stuck "
                         "in a loop. Consider stopping if it doesn't recover."
                         % (self._action_repeat_streak, name or "action"))})

    def _note_token_usage(self, in_tokens) -> None:
        try:
            it = int(in_tokens)
        except (TypeError, ValueError):
            return
        if it <= 0:
            return
        if it > self._peak_in_tokens:
            self._peak_in_tokens = it
        self._cum_in_tokens += it
        if it >= self._TOKEN_WARN_THRESHOLD and not self._tok_warned:
            self._tok_warned = True
            self.messages.append({"ts": time.time(), "role": "error",
                "text": ("⚠️ High token use — this turn is sending ~%d input tokens "
                         "(accumulated screenshots/context). Vision-heavy/long tasks burn "
                         "your subscription rate limit fast; consider stopping if it's "
                         "looping." % it)})
        # governor hard cap (#34 phase A): the warn above fired during the 89M-token
        # lichess run and protected nothing — nobody watches a headless trace. On a
        # cap trip, stop the run exactly like a human Stop tap, reason in the trace.
        if self._tok_stop_fired:
            return
        turn_cap, run_cap = _tok_caps()
        reason = ""
        if turn_cap and it >= turn_cap:
            reason = ("this turn re-sent ~%s input tokens (per-turn cap %s)"
                      % (f"{it:,}", f"{turn_cap:,}"))
        elif run_cap and self._cum_in_tokens >= run_cap:
            reason = ("this run has consumed ~%s cumulative input tokens (per-run cap %s)"
                      % (f"{self._cum_in_tokens:,}", f"{run_cap:,}"))
        if not reason:
            return
        self._tok_stop_fired = True
        self.messages.append({"ts": time.time(), "role": "error",
            "text": ("⛔ Token cap hit — auto-stopping the run: " + reason +
                     ". Tune with OPERATOR_TOKEN_TURN_STOP / OPERATOR_TOKEN_RUN_STOP "
                     "(0 disables).")})
        try:
            self.stop()
        except Exception:  # noqa: BLE001 — a failed stop must not kill the reader thread
            pass
        # AFTER stop() — it clears handoff; the banner must survive the stop.
        self.handoff = {"reason": "Token cap auto-stop: " + reason,
                        "ts": time.time()}


    def _agy_apply_loop_nudge(self, task: str) -> str:
        """#40b reactive half: if the previous agy run tripped the overthink-loop
        guard, prepend a one-shot nudge to THIS turn's task so the model is told
        to act-or-answer rather than re-reason. Consume-once: clears the flag so
        only the immediately-following turn is nudged. No-op when unarmed."""
        if not getattr(self, "_agy_loop_nudge_pending", False):
            return task
        self._agy_loop_nudge_pending = False
        nudge = (
            "[Heads-up from the system: on the last turn you got stuck reasoning "
            "in a loop — several steps in a row with no browser action, re-describing "
            "what you already saw. This turn: take a concrete action immediately, or "
            "if you already have enough to answer, just give the answer. Don't "
            "re-describe the screen instead of acting.]\n\n"
        )
        return nudge + task

    def _apply_loop_nudge(self, task: str) -> str:
        """Consume-once nudges for ALL runtimes (§2.1): the agy think-loop nudge
        plus the runtime-agnostic repeat-action nudge. There is no mid-run input
        channel (stdin=DEVNULL everywhere), so a loop detected on turn N can only
        be corrected at the top of turn N+1 — this is that correction."""
        task = self._agy_apply_loop_nudge(task)
        if not self._repeat_nudge_pending:
            return task
        self._repeat_nudge_pending = False
        return (
            "[Heads-up from the system: on the last turn you repeated the SAME "
            "action several times in a row with no effect — the classic stuck "
            "loop (e.g. re-clicking coords that don't work). This turn: do NOT "
            "retry the same action blind. Look at the screen again first "
            "(screenshot/perceive), diagnose why it didn't work (wrong coords? a "
            "popup in the way? wrong element?), and try a DIFFERENT approach — "
            "or if you're blocked, say so / ask for a takeover.]\n\n"
        ) + task

    # §3.3 completion gate + bounded auto-replan. There is no mid-run input
    # channel, so the gate is a follow-up TURN: on a clean exit that either
    # (a) lacks recent visual evidence of the outcome, or (b) reads like the
    # agent bailed with the task unfinished, _run_inner returns a gate prompt
    # instead of setting `done`, and _run runs exactly one more resumed turn.
    # Hard-bounded: one follow-up per start() (_gate_fired), never in demo,
    # never after a stop/token-cap/handoff. Kill-switch OPERATOR_COMPLETION_GATE=0.
    _GATE_EVIDENCE_WINDOW = 2   # consequential acts allowed after the last look
    _BAIL_MARKERS = ("unable to", "cannot ", "can't ", "couldn't", "i'll stop",
                     "stopping here", "give up", "giving up", "not possible",
                     "failed to complete", "blocked by", "take over",
                     "please intervene")
    _GATE_VERIFY_PROMPT = _prompts.GATE_VERIFY_PROMPT
    _GATE_REPLAN_PROMPT = _prompts.GATE_REPLAN_PROMPT

    def _completion_gate_check(self) -> str:
        """Gate prompt for a follow-up turn, or '' to accept `done` as-is."""
        if os.environ.get("OPERATOR_COMPLETION_GATE", "1") == "0":
            return ""
        if self._gate_fired or getattr(self, "demo", False):
            return ""
        if getattr(self, "_stopped", False) or self._tok_stop_fired:
            return ""
        if self.handoff:            # deliberate takeover request — not a bail
            return ""
        if self._consequential_acts < 1:
            return ""               # chat/read-only turn — nothing to verify
        final = next((m["text"] for m in reversed(self.messages)
                      if m.get("role") == "assistant" and m.get("text")), "")
        if any(k in final.lower() for k in self._BAIL_MARKERS):
            self._gate_fired = True
            self.messages.append({"ts": time.time(), "role": "error",
                "text": ("🔁 Auto-replan — the run ended sounding blocked; "
                         "asking the agent to try one more approach.")})
            return self._GATE_REPLAN_PROMPT
        if self._acts_since_visual > self._GATE_EVIDENCE_WINDOW:
            self._gate_fired = True
            self.messages.append({"ts": time.time(), "role": "error",
                "text": ("🔎 Completion check — the run ended without a final "
                         "look at the screen; asking the agent to verify "
                         "before accepting done.")})
            return self._GATE_VERIFY_PROMPT
        return ""

    def steer(self, text: str) -> dict:
        """Queue a mid-run message for the LIVE run (1.0.12). Delivery is
        layered: the PostToolUse steer hook injects it right after the agent's
        next tool call (claude runtime, mid-loop), and whatever the hook didn't
        consume becomes one more resumed turn at the exit seam
        (_steer_followup_check — the only seam codex/agy have). Not running →
        not ok, and the client falls back to a normal dispatch."""
        if not self.is_running():
            return {"ok": False, "error": "nothing running"}
        try:
            n = operator_steer.push(text)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        text = text.strip()
        # visible in the run trace (and thus the history ledger), and part of
        # the shared transcript so a future cold-start inject carries it too
        self.messages.append({"ts": time.time(), "role": "user", "text": text})
        self._transcript.append({"role": "user", "text": text})
        self._transcript = self._transcript[-40:]
        self._save_state()
        self._touch()
        return {"ok": True, "queued": n,
                "live": (getattr(self, "_runtime", "") == "claude"
                         and not getattr(self, "demo", False))}

    def _steer_followup_check(self) -> str:
        """Exit-seam steer delivery (1.0.12): a clean exit with steers still
        queued returns a follow-up prompt instead of landing `done` — the
        §3.3 gate's mechanics (prompt return + _gate_pending across the
        proc-less gap), but user-driven, so _run's loop repeats it until the
        queue is dry rather than firing once. Never after a stop/cancel/token
        cap, never in demo."""
        if getattr(self, "demo", False) or getattr(self, "_stopped", False):
            return ""
        if self._tok_stop_fired or self._cancel_requested:
            return ""
        steers = operator_steer.take_all()
        if not steers:
            return ""
        self._gate_pending = True
        self._touch()
        return operator_steer.followup_prompt(steers)

    def _consume_codex(self, evt: dict) -> None:
        """Parse one codex `exec --json` JSONL event into messages."""
        # T1: same contract as _consume — codex owns these field types, we
        # don't. Drop non-conforming shapes instead of leaking an exception
        # into the reader thread (which would silently wedge the run).
        t = evt.get("type")
        if t == "thread.started":
            tid = evt.get("thread_id")
            if isinstance(tid, str) and tid:
                self._cur_session = tid          # codex resume id
        elif t == "item.completed":
            item = evt.get("item")
            item = item if isinstance(item, dict) else {}
            it = item.get("type")
            if it == "agent_message":
                txt = item.get("text")
                txt = txt.strip() if isinstance(txt, str) else ""
                if txt:
                    txt, _reason = _extract_handoff(txt)
                    if _reason is not None:
                        self.handoff = {"reason": _reason, "ts": time.time()}
                    if txt:
                        self.messages.append({"ts": time.time(), "role": "assistant", "text": txt})
            elif it in ("mcp_tool_call", "tool_call", "function_call"):
                # surface browser actions inline (Operator-style trace)
                name = item.get("tool") or item.get("name")
                name = name if isinstance(name, str) else ""
                args = item.get("arguments") or item.get("input") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                args = args if isinstance(args, dict) else {}
                self._note_action(name, args)
                label, detail = _action_label(name, args)
                if label:
                    self.messages.append({"ts": time.time(), "role": "action",
                                          "text": label, "detail": detail})
            elif it == "command_execution":
                cmd = item.get("command")
                cmd = cmd.strip() if isinstance(cmd, str) else ""
                if cmd:
                    self._note_action("command", cmd)
                    self.messages.append({"ts": time.time(), "role": "action",
                                          "text": "Running command", "detail": cmd[:70]})
        elif t == "token_count":
            # codex stream-json emits cumulative token usage; the last_token_usage /
            # info carries this turn's input size — guard against runaway context.
            info = evt.get("info") or evt.get("usage") or evt
            info = info if isinstance(info, dict) else {}
            _it = (info.get("last_token_usage", {}) or {}).get("input_tokens") \
                if isinstance(info.get("last_token_usage"), dict) else None
            if _it is None:
                _it = info.get("input_tokens") or info.get("total_tokens")
            self._note_token_usage(_it)
        elif t == "error":
            msg = evt.get("message") or evt.get("error")
            msg = msg.strip() if isinstance(msg, str) else ""
            if msg:
                self.messages.append({"ts": time.time(), "role": "error", "text": msg[:200]})

    def _agy_snapshot_trajectories(self) -> dict:
        return operator_agy.snapshot_trajectories(self._agy_brain_dir)

    def _agy_find_trajectory(self, strict: bool = False) -> str | None:
        """Thin hook — see operator_agy.find_trajectory for the strict/live
        vs final-flush selection rules."""
        return operator_agy.find_trajectory(
            self._agy_brain_dir, self._agy_traj_before or {}, strict=strict)

    def _agy_parse_trajectory(self, path: str) -> bool:
        """Thin hook: the trajectory parser lives in operator_agy (R5); this
        runner is the sink it streams messages/loop-guard state into."""
        return operator_agy.parse_trajectory(path, self)

    def _start_agy_live_poll(self):
        """agy -p doesn't stream, but it writes transcript_full.jsonl incrementally.
        Poll it during the run and parse-incrementally so thinking/actions show LIVE
        (the dedupe set means each parse only emits new steps). Best-effort daemon;
        the post-wait _flush_agy still does the final authoritative parse."""
        def _poll():
            import time as _t
            locked = None   # once we find THIS run's trajectory, stick to it — re-picking
                            # the "freshest" each poll can flip between files (an older
                            # brain dir with a recent mtime), which stalls streaming until
                            # the very end. Lock on first find for consistent live steps.
            while self._proc and self._proc.poll() is None:
                try:
                    if locked is None:
                        locked = self._agy_find_trajectory(strict=True)
                    if locked:
                        self._agy_parse_trajectory(locked)
                        self._agy_live_traj = locked   # let the final flush reuse the same file
                except Exception:
                    pass
                _t.sleep(0.4)   # tight poll → gemma tool-calls show near-live (parse is cheap: read jsonl + dedupe)
        try:
            threading.Thread(target=_poll, daemon=True, name="agy-live-trace").start()
        except Exception:
            pass

    def _flush_agy(self) -> None:
        """Surface agy's output. Primary: parse the structured trajectory
        (transcript_full.jsonl) for the FULL thinking + tool-call trace — parity with
        codex/claude. Fallback: the plain-text `-p` stdout (clean final answer) if the
        trajectory can't be found/parsed. De-dupe so the final answer isn't posted twice
        (trajectory final == stdout final). Best-effort: never crash the run."""
        stdout_text = "\n".join(self._agy_buf).strip()
        self._agy_buf = []

        parsed_answer = False
        try:
            traj = self._agy_find_trajectory()
            if traj:
                parsed_answer = self._agy_parse_trajectory(traj)
        except Exception:  # noqa: BLE001 — trace is best-effort; stdout is the floor
            parsed_answer = False

        # The trajectory's FINAL planner content is the authoritative final answer (the
        # plain `-p` stdout sometimes captures an intermediate thinking line, not the
        # clean reply). So when the trajectory yielded an answer, trust it and DON'T also
        # post the stdout — that's what caused a stray reasoning line to tail the trace.
        # The stdout fallback fires ONLY when the trajectory couldn't be found/parsed.
        if parsed_answer or not stdout_text:
            return
        # drop agy interrupt/timeout noise (user Stop) — never a reply
        stdout_text = operator_agy.filter_stop_noise(stdout_text)
        if getattr(self, "_stopped", False) and not stdout_text:
            return   # interrupted run produced only noise → emit nothing
        if not stdout_text:
            return
        text, _reason = _extract_handoff(_clean_gemma_text(stdout_text))
        if _reason is not None and not self.handoff:
            self.handoff = {"reason": _reason, "ts": time.time()}
        if text:
            self.messages.append({"ts": time.time(), "role": "assistant", "text": text})

    def reset_session(self, bot: str = "") -> dict:
        """Forget stored session id(s) + the shared transcript so the next task
        starts a fresh conversation (wired to the operator's clear/trash button).
        Also force-clears a wedged "running" state whose process is already
        dead — the trash button is the manual escape hatch for that hang."""
        # if state says running but the process is gone, the _run finally never
        # landed; unwedge it so the reset (and the next dispatch) isn't rejected.
        if self.state == "running" and (self._proc is None or self._proc.poll() is not None):
            self._proc = None
            self._set_state("idle", "reset: unwedged stale running")
        if bot:
            self._session_ids.pop(bot, None)
        else:
            self._session_ids.clear()
        self._transcript = []
        self._last_bot = None
        self.handoff = None
        self._save_state()
        return {"ok": True}

    def _resolve_terminal(self, returncode: int) -> tuple[str, str]:
        """Terminal (state, reason) for a finished run. The stop flags are
        written from three threads (poll-thread watchdog, run-thread token
        cap, request-thread user Stop) — read them under the lock and apply
        ONE priority so a race can't mislabel the run (B4):
        token-cap > stall-kill > user stop > exit code."""
        with self._lock:
            tok_capped = self._tok_stop_fired
            stall_reason = self._stall_kill_reason
            stopped = self._stopped
        if tok_capped:
            # the handoff banner set by _note_token_usage carries the detail
            return ("interrupted", "token cap auto-stop")
        if stall_reason:
            # not a user stop — the watchdog killed it. Surface a real error
            # with the reason instead of a quiet "Interrupted".
            return ("error", stall_reason)
        if stopped:
            return ("interrupted", "user stop")
        if returncode == 0:
            return ("done", "exit 0")
        return ("error", f"exit {returncode}")

    def stop(self) -> dict:
        p = self._proc
        self.handoff = None   # a takeover/stop clears any pending handoff request
        try:
            operator_steer.clear()   # a stop abandons queued steers with the run
        except Exception:  # noqa: BLE001
            pass
        self._stopped = True  # so _flush_agy drops agy's interrupt-noise stdout
        # B3: survives the follow-up turn's per-run reset of _stopped; the §3.3
        # dispatch in _run gates on this, and only start() clears it.
        self._cancel_requested = True
        # Arm the control-layer kill switch FIRST: any in-flight macro/desktop
        # injection halts on its next op, even before the process tree dies.
        # Safe for later runs — surfaces only honor a stop newer than their own
        # start (see control/surfaces.py).
        try:
            _stop_path = os.path.expanduser(
                "~/.cache/computer-use/operator-stop.json")
            os.makedirs(os.path.dirname(_stop_path), exist_ok=True)
            with open(_stop_path, "w", encoding="utf-8") as _f:
                json.dump({"ts": time.time()}, _f)
        except OSError:
            pass
        if p and self.is_running():
            import signal as _sig
            # kill the whole process GROUP — codex/agy spawns children (the MCP server,
            # node, bwrap) that keep running + emitting traces after a bare terminate()
            # of just the leader (the "stop doesn't fully stop" bug). SIGTERM the group,
            # then SIGKILL anything still alive a moment later.
            try:
                _pgid = os.getpgid(p.pid)
                os.killpg(_pgid, _sig.SIGTERM)
                def _hard_kill(pgid=_pgid):
                    import time as _t
                    _t.sleep(1.0)
                    try: os.killpg(pgid, _sig.SIGKILL)
                    except Exception: pass
                threading.Thread(target=_hard_kill, daemon=True).start()
            except Exception:  # noqa: BLE001 — fall back to terminating the leader
                try: p.terminate()
                except Exception: pass
            # REAP THE STDIO MCP. claude -p / codex spawn the Playwright MCP as a stdio
            # child that often survives the process-group kill (it re-parents / detaches),
            # leaving a node cli.js still attached to the CDP page. The NEXT turn's MCP
            # then contends with the zombie for the same page → the agent hangs after the
            # first turn (the stuck-after-interrupt bug). Kill ONLY the Operator-spawned
            # stdio MCP — signature: cli.js with --caps + --cdp-endpoint and NO --port
            # (the persistent :8772 HTTP MCP HAS --port, so it's untouched).
            try:
                import subprocess as _sp, signal as _sig2
                out = _sp.run(["ps", "-eo", "pid=,args="], capture_output=True,
                              text=True, timeout=5).stdout
                for ln in out.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    pid_s, _, args = ln.partition(" ")
                    # the Operator-spawned stdio MCP: the playwright cli.js with --caps
                    # and --cdp-endpoint but NO --port (the persistent :8772 HTTP MCP HAS
                    # --port, so it's spared). Also reap the wrapper script if lingering.
                    is_stdio_mcp = ("@playwright/mcp/cli.js" in args
                                    and "--caps" in args and "--cdp-endpoint" in args
                                    and "--port" not in args)
                    is_wrapper = "browse/playwright-mcp.sh" in args
                    if is_stdio_mcp or is_wrapper:
                        try: os.kill(int(pid_s), _sig2.SIGKILL)
                        except Exception: pass
            except Exception:
                pass
            # Do NOT write a state here: the _run thread is still alive and will
            # land its own terminal state ("interrupted", via _stopped) once the
            # killed process reaps. Writing "idle" here raced that write — last
            # writer won, so a stop sometimes read idle, sometimes interrupted.
            return {"ok": True}
        # B3: the gate gap has no proc to kill, but the run thread is alive and
        # about to consult _cancel_requested — it lands "interrupted" itself.
        # Unwedging here would flip a legitimately-running run to idle and race
        # a fresh dispatch against the still-live thread.
        if self.state == "running" and getattr(self, "_gate_pending", False):
            return {"ok": True, "cancelled_followup": True}
        # §2.3: a stale 'running' (dead process, _run finally never landed) used
        # to make Stop answer "nothing running" while the UI still showed a run
        # — Reset was the only way out. Stop is a recovery action: unwedge here.
        # BUT only when the run thread is actually dead: a live thread with no
        # proc is the PRE-SPAWN window, and unwedging that flips a genuinely-
        # spawning run to idle and orphans its process. There the flags set
        # above (_stopped/_cancel_requested) are the stop — _run_inner's
        # post-spawn check reaps the process the moment it exists.
        if self.state == "running":
            t = self._thread
            if t is not None and t.is_alive():
                return {"ok": True, "cancelled_prespawn": True}
            self._proc = None
            self._set_state("idle", "stop: unwedged stale running")
            return {"ok": True, "unwedged": True}
        return {"ok": False, "error": "nothing running"}

    def snapshot(self, since_ts: float = 0.0) -> dict:
        # ── stall watchdog (§2.1) — piggybacks on the status poll ──────────
        # `alive` distinguishes dead from silent; this distinguishes silent-but-
        # working from silent-and-STUCK: no progress heartbeat (no output line,
        # no trace step) for longer than the soft budget → report stalled; past
        # the hard budget → auto-stop like a human Stop tap, marked error with
        # the real reason (the _run terminal branch reads _stall_kill_reason).
        # B2: the _gate_pending gap is a legitimately quiet window — turn 1's
        # heartbeat is stale while turn 2 spawns — never read it as a stall.
        stalled = False
        stalled_for = 0.0
        if (self.state == "running" and self.is_running()
                and self.last_progress_ts and not self._gate_pending):
            stalled_for = max(0.0, time.time() - self.last_progress_ts)
            soft, hard = _stall_budgets()
            stalled = bool(soft and stalled_for > soft)
            fire = False
            if hard and stalled_for > hard:
                # B4: check-and-set under the lock — two racing status polls
                # must not both claim the kill and double-fire stop()
                with self._lock:
                    if not self._stall_kill_reason:
                        self._stall_kill_reason = (
                            f"stalled: no progress for {int(stalled_for)}s — watchdog auto-stop")
                        self.messages.append({"ts": time.time(), "role": "error",
                            "text": "⏱ " + self._stall_kill_reason})
                        fire = True
                if fire:
                    _log.warning("operator %s", self._stall_kill_reason)
                    self.stop()
        # B1: the run thread appends to messages while this poll thread reads —
        # iterate a private copy taken under the lock. (list.append itself is
        # GIL-atomic; the torn reads were iteration/serialization mid-append.)
        with self._lock:
            msgs = self.messages[:]
        # `final` = the last assistant text of THIS turn, unfiltered by since_ts, so
        # the client can always render the reply bubble even if it missed the
        # incremental message or the agent emitted it right at turn-end (codex/gpt
        # sometimes flushes the final agent_message together with turn completion).
        final = next((m["text"] for m in reversed(msgs)
                      if m.get("role") == "assistant" and m.get("text")), "")
        return {
            "bot": self.bot, "task": self.task, "state": self.state,
            "started_ts": self.started_ts, "ended_ts": self.ended_ts,
            "messages": [m for m in msgs if m["ts"] > since_ts],
            "final": final,
            # `alive` = the agent subprocess is genuinely still running (poll()==None),
            # not just "state says running." The client's stall watchdog uses this to
            # tell a SILENT-BUT-WORKING agent (a long reasoning step, a slow page, a
            # natural mid-convo pause) from a truly DEAD/wedged one. Only the latter
            # should trip "the agent stalled" — a live process is making progress even
            # when it emits no new message line for a while.
            "alive": self.is_running(),
            # §2.1 server-side stall signal — the client renders this instead of
            # guessing stalls from message gaps (its old false-kill failure mode).
            "stalled": stalled,
            "stalled_for": round(stalled_for, 1),
            "handoff": self.handoff,   # #4: {reason, ts} when the agent asks for a takeover
            "surface": getattr(self, "surface", "browser"),
            # 1.0.12: steers queued but not yet consumed by a delivery seam —
            # the client renders "queued" until this drops back to 0.
            "steer_pending": len(operator_steer.pending()),
            # 1.0.15 live run economics: the ledger's token numbers, visible
            # WHILE the run burns (same _note_token_usage basis — cache reads
            # excluded, so these read small vs the raw API meter). NB both
            # reset per TURN (_run_inner), so on a gate/steer follow-up the
            # meter shows the current turn's burn, not the whole run's.
            "cum_in_tokens": self._cum_in_tokens,
            "peak_in_tokens": self._peak_in_tokens,
        }


runner = AgentRunner()
