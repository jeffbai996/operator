"""operator_agent.py — run a headless Claude Code agent that drives the browser.

Option 1 (Jeff 2026-06-26): the operator IS the agent. We spawn `claude -p` in a
background thread, as the chosen persona, with the Playwright MCP pointed at the
SAME logged-in Chrome the operator views — authenticated on the Max SUBSCRIPTION
(claude reads ~/.claude/.credentials.json), zero metered API spend. We parse its
stream-json output live: assistant text → the operator chat, browser tool calls
→ the action trail. No Discord, no live-session dependency, no spam.

Drivers run a logged-in CLI (claude / codex) headless — no metered API key.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

# personas that can drive + the config dir whose stored sub-creds + identity they
# run under. (Both ride the default ~/.claude credentials = the Max login.)
_BROWSER_MANDATE = (
    " You are operating a LIVE web browser via your Playwright tools — that is your"
    " primary tool and the WHOLE POINT of this session."
    " DEFAULT TO BROWSING. For ~99% of requests, your first move is to USE THE"
    " BROWSER — navigate, read real pages, and answer from what you actually see."
    " Assume the user wants a live, browser-derived answer unless it is OBVIOUSLY"
    " not a browsing task. When in doubt, BROWSE — never answer from memory just"
    " because you think you know; verify on a real page."
    " The only times you may answer directly WITHOUT browsing:"
    " (a) a pure conversational/meta reply (e.g. 'which bot are you?', 'what can"
    " you do?', a greeting);"
    " (b) the user is clearly asking about what is ALREADY on the current page"
    " (seeded from the operator screenshot);"
    " (c) a trivial self-contained computation or definition with no real-world"
    " or time-sensitive component."
    " Everything else — prices, scores, availability, news, products, facts,"
    " 'look up', 'find', 'what's X', 'is X open', research, comparisons — you MUST"
    " browse and base the answer ONLY on the pages you visited. Do NOT say you"
    " can't browse — you can. Cite the pages you actually visited."
)
# The drivers Operator can run. Both are BYO-subscription: each shells out to a
# logged-in CLI (no metered API key) -- the primary, cheapest path:
#   claude -> Claude Code  (reads ~/.claude credentials; install the `claude` CLI + `claude login`)
#   gpt    -> OpenAI codex (reads ~/.codex credentials; install the `codex` CLI + sign in)
# Config dirs are overridable via CLAUDE_CONFIG_DIR / CODEX_HOME. To use the metered
# API fallback instead, set a key in .env (see .env.example).
AGENT_BOTS = {
    "claude": {"label": "claude", "runtime": "claude",
               "config_dir": os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
               "cwd": os.path.expanduser("~"),
               "persona": "You are a helpful, capable computer-using assistant." + _BROWSER_MANDATE},
    "gpt": {"label": "gpt", "runtime": "codex",
            "config_dir": os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
            "cwd": os.path.expanduser("~"),
            "persona": "You are a helpful, capable computer-using assistant." + _BROWSER_MANDATE},
}

_BROWSE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browse")
# MCP config that gives the agent the Playwright tools, attached to :9222 Chrome
# via the stdio wrapper in browse/playwright-mcp.sh.
_MCP_CONFIG = {
    "mcpServers": {
        "playwright": {"command": "bash", "args": [os.path.join(_BROWSE, "playwright-mcp.sh")]}
    }
}


# Map a Playwright MCP tool call -> ("present-tense action label", "short detail")
# so the operator trace can interleave actions with the agent's thinking.
_ACTION_LABELS = {
    "browser_click": "Clicking", "browser_double_click": "Double-clicking",
    "browser_type": "Typing", "browser_navigate": "Browsing",
    "browser_navigate_back": "Going back", "browser_navigate_forward": "Going forward",
    "browser_press_key": "Pressing", "browser_scroll": "Scrolling",
    "browser_select_option": "Selecting", "browser_hover": "Hovering",
    "browser_take_screenshot": "Took screenshot", "browser_screenshot": "Took screenshot",
    "browser_snapshot": "Reading", "browser_get_text": "Reading", "browser_read": "Reading",
    "browser_wait_for": "Waiting", "browser_wait": "Waiting",
    "browser_file_upload": "Uploading", "browser_tabs": "Switching tab",
    "browser_tab_new": "Opening tab", "browser_tab_close": "Closing tab",
    "browser_fill_form": "Filling form", "browser_fill": "Filling",
    "browser_drag": "Dragging", "browser_drag_and_drop": "Dragging",
    "browser_evaluate": "Reading", "browser_run_code_unsafe": "Reading",
    "browser_handle_dialog": "Handling dialog", "browser_dialog": "Handling dialog",
    "browser_close": "Closing", "browser_resize": "Resizing",
    "browser_console_messages": "Reading console", "browser_network_requests": "Inspecting network",
    "browser_pdf_save": "Saving PDF", "browser_go_back": "Going back", "browser_go_forward": "Going forward",
}


# non-browser tools the agent might call (web search, fetch, etc.) — show these in
# the trace too so the user sees "Searching…" not just "Thinking".
_NONBROWSER_LABELS = {
    # web
    "websearch": "Searching", "web_search": "Searching", "search": "Searching",
    "webfetch": "Fetching", "web_fetch": "Fetching", "fetch": "Fetching",
    # shell / files
    "bash": "Running command", "read": "Reading file", "grep": "Searching files",
    "glob": "Finding files", "write": "Writing file", "edit": "Editing file",
    "multiedit": "Editing file", "notebookedit": "Editing notebook",
    "ls": "Listing files", "cat": "Reading file",
    # memory / recall
    "recall": "Recalling", "memory": "Checking memory", "vecgrep": "Searching memory",
    "get_corpus": "Searching memory", "list_corpora": "Checking memory",
    # markets (ibkr)
    "get_quote": "Checking quote", "quote": "Checking quote",
    "get_positions": "Checking portfolio", "ibkr_quote": "Checking quote",
    "ibkr_get_positions": "Checking portfolio", "ibkr_get_account_summary": "Checking account",
    "ibkr_margin": "Checking margin", "ibkr_get_historical_bars": "Pulling chart data",
    # docs / misc
    "query-docs": "Reading docs", "resolve-library-id": "Looking up library",
    "task": "Delegating", "todowrite": "Updating todos", "webfetch_url": "Fetching",
    "fill_form": "Filling form",
    # discord MCP (bots fetch/reply/react in-channel)
    "fetch_messages": "Fetching messages", "reply": "Replying", "send_message": "Sending message",
    "react": "Reacting", "edit_message": "Editing message", "download_attachment": "Downloading",
    "set_presence": "Setting presence", "delete_message": "Deleting message",
}

# verbs whose -ing form we can build mechanically, so an unknown verb_noun tool
# (e.g. "fetch_messages") still reads as present-continuous ("Fetching messages")
# instead of the clunky "Using fetch messages".
_GERUND_VERBS = {
    "fetch": "Fetching", "get": "Getting", "send": "Sending", "list": "Listing",
    "create": "Creating", "make": "Making", "update": "Updating", "edit": "Editing",
    "delete": "Deleting", "remove": "Removing", "search": "Searching", "find": "Finding",
    "read": "Reading", "write": "Writing", "run": "Running", "open": "Opening",
    "close": "Closing", "add": "Adding", "set": "Setting", "check": "Checking",
    "load": "Loading", "save": "Saving", "download": "Downloading", "upload": "Uploading",
    "navigate": "Navigating", "click": "Clicking", "type": "Typing", "select": "Selecting",
    "react": "Reacting", "reply": "Replying", "post": "Posting", "pull": "Pulling",
    "push": "Pushing", "query": "Querying", "resolve": "Resolving", "build": "Building",
    "start": "Starting", "stop": "Stopping", "call": "Calling", "view": "Viewing",
}


def _gerund_label(bare: str) -> str:
    """Best-effort present-continuous label for an unknown tool name.
    'fetch_messages' -> 'Fetching messages'; falls back to '' if the first token
    isn't a known verb (caller then uses the code-block 'Using `tool`' form)."""
    parts = [w for w in bare.replace("-", "_").split("_") if w]
    if not parts:
        return ""
    head = _GERUND_VERBS.get(parts[0].lower())
    if not head:
        return ""
    rest = " ".join(parts[1:])
    return (head + " " + rest).strip()
# tools that are pure plumbing — never show them as actions.
_SKIP_TOOLS = {"toolsearch", "tooldispatch"}


def _action_label(tool: str, args: dict) -> tuple[str, str]:
    """browser_* tool + input -> (label, detail). Non-browser tools -> ('', '').

    Tool names arrive MCP-namespaced (e.g. 'mcp__playwright__browser_navigate') —
    strip that prefix before matching, else nothing ever registers as an action
    (the bug that made the trace show only 'Thinking', never the click/nav steps)."""
    if not isinstance(tool, str):
        return "", ""
    bare = tool
    if "__" in bare:
        bare = bare.rsplit("__", 1)[-1]   # mcp__playwright__browser_navigate -> browser_navigate
    low = bare.lower()
    if not bare.startswith("browser_"):
        if low in _SKIP_TOOLS:
            return "", ""
        nb = _NONBROWSER_LABELS.get(low)
        if nb is None:
            # try to generalize to present-continuous ("fetch_messages" -> "Fetching
            # messages"); if the first token isn't a known verb, fall back to the
            # code-block form ("Using `tool_name`") the trace renders as a code chip.
            nb = _gerund_label(bare)
            if not nb:
                nb = ("Using `" + bare + "`") if bare else ""
        if nb:
            d = ""
            if isinstance(args, dict):
                for k in ("query", "url", "command", "pattern", "prompt", "symbol",
                          "q", "text", "name", "path", "file_path", "id"):
                    v = args.get(k)
                    if isinstance(v, str) and v.strip():
                        d = v.strip()[:120]; break
            return nb, d
        return "", ""
    label = _ACTION_LABELS.get(bare, bare.replace("browser_", "").replace("_", " ").capitalize())
    detail = ""
    if isinstance(args, dict):
        for k in ("element", "url", "text", "value", "key", "selector", "query", "ref"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                detail = v.strip()[:120]
                break
        if not detail and ("width" in args or "height" in args):   # screenshot → resolution
            w, h = args.get("width"), args.get("height")
            if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                detail = f"{int(w)}×{int(h)}"
        if not detail:                      # Waiting: surface the time, humanized
            for k in ("time", "timeout", "seconds", "ms"):
                v = args.get(k)
                if isinstance(v, (int, float)):
                    secs = v / 1000.0 if k == "ms" else float(v)
                    detail = _fmt_duration(secs)
                    break
    return label, detail


def _fmt_duration(secs: float) -> str:
    """Humanize a seconds value: 2000 -> '33m 20s', 90 -> '1m 30s', 5 -> '5s',
    0.5 -> '500ms'. Sub-second shows ms; whole minutes drop the '0s'."""
    if secs < 1:
        return f"{int(round(secs * 1000))}ms"
    secs = int(round(secs))
    if secs < 60:
        return f"{secs}s"
    m, sec = divmod(secs, 60)
    if m < 60:
        return f"{m}m {sec}s" if sec else f"{m}m"
    h, m = divmod(m, 60)
    parts = f"{h}h"
    if m:
        parts += f" {m}m"
    if sec:
        parts += f" {sec}s"
    return parts


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
        self.messages: list = []          # [{ts, role, text}] reasoning/replies
        self.started_ts: float = 0.0
        self.model: str = ''
        self.effort: str = ''
        self.ended_ts: float = 0.0
        self._cur_session: str = ''       # session id captured this run
        # SHARED conversation transcript across ALL bots (runtime-agnostic) so the
        # convo survives switching claude↔claude↔gpt. [{role:'user'|'assistant', text}]
        # Persisted to disk so it ALSO survives a server restart (the store/Flask
        # process bounces on every deploy — without this the convo evaporated).
        self._state_path = os.path.join(
            os.path.expanduser("~/.cache/computer-use"), "operator-state.json")
        self._session_ids: dict = {}      # bot -> last claude session id (resume)
        self._transcript: list = []
        self._last_bot: str | None = None
        self._load_state()

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
        return self.state == "running"

    def start(self, bot: str, task: str, model: str = '', effort: str = '') -> dict:
        with self._lock:
            if self.is_running():
                return {"ok": False, "error": f"{self.bot} is already running a task"}
            b = AGENT_BOTS.get(bot)
            if not b:
                return {"ok": False, "error": f"'{bot}' can't drive"}
            runtime = b.get("runtime", "claude")
            if runtime == "codex":
                binpath = _resolve_codex()
                if not binpath:
                    return {"ok": False, "error": "codex binary not found"}
            else:
                binpath = _resolve_claude()
                if not binpath:
                    return {"ok": False, "error": "claude binary not found"}
            self._switched_bot = (self._last_bot is not None and self._last_bot != bot)
            self.bot, self.task = bot, task
            self.state = "running"
            self.messages = []
            self._transcript.append({"role": "user", "text": task})
            self._transcript = self._transcript[-40:]
            self._save_state()   # cap
            self.started_ts = time.time()
            self.ended_ts = 0.0
            self.model, self.effort = (model or '').strip(), (effort or '').strip()
            # default the claude runtime to Sonnet 4.6 / medium when nothing was picked
            # (empty model would otherwise drop the flag and use the CLI's own default).
            if b.get("runtime") == "claude":
                if not self.model:  self.model = "sonnet"
                if not self.effort: self.effort = "medium"
            self._thread = threading.Thread(target=self._run, args=(binpath, b, task),
                                            daemon=True, name="operator-agent")
            self._thread.start()
            return {"ok": True, "bot": bot}

    def _run(self, binpath: str, b: dict, task: str) -> None:
        self._runtime = b.get("runtime", "claude")
        self._cur_session = ""
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
        # Reinforce browser-first ON the task text (models weight the prompt heavily,
        # esp. codex/GPT). Skip for obviously-conversational asks.
        _convo = task.strip().lower()
        _is_chatty = (len(_convo) < 40 and any(_convo.startswith(w) for w in
            ("hi", "hey", "hello", "yo", "thanks", "thank you", "who are you",
             "which bot", "what can you")))
        if not _is_chatty:
            task = (
                "SYSTEM DIRECTIVE — READ FIRST. You are driving a LIVE web browser the "
                "user is watching in real time. You have Playwright browser tools. For "
                "this request you MUST act IN THE BROWSER — call your browser tools "
                "(navigate / click / type / read the page). DO NOT just reply with text. "
                "DO NOT answer from memory. If the request references something on a page "
                "or a site (e.g. 'respond to ernie', 'reply to X', 'search Y', 'check Z'), "
                "that means GO DO IT in the browser — find the relevant tab/site, take the "
                "action, and confirm what you did. A text-only answer with no browser tool "
                "calls is a FAILURE. Begin by using a browser tool now. "
                "THEN, after you've acted, ALWAYS end with a short final answer that "
                "tells the user what you found or did (e.g. the scores, the price, the "
                "result) — don't just go silent after the actions. The only time you may "
                "skip the summary is if it was patently a do-it-and-leave action with "
                "nothing to report back. "
                "FORMATTING: in your final answer, wrap numeric/financial figures, prices, "
                "tables, and any data rows in a ``` code block ``` for readability; bold the "
                "key takeaway. Keep prose outside code blocks. "
                "TABS: navigate IN THE CURRENT TAB by default — don't open a new tab for "
                "each step. Only open a new tab if you genuinely need two pages side by side; "
                "otherwise reuse the active tab so the user's view follows you. "
                "VISION vs DOM: default to browser_snapshot (the text/DOM tree) — it's faster "
                "and right for MOST tasks (reading text, filling forms, clicking links). BUT "
                "snapshot is BLIND to images, maps, video, canvas, and game graphics: it only "
                "sees text/markup. So when the answer depends on what something LOOKS like — "
                "GeoGuessr, reading a chart/map/photo, judging a layout, any visual judgment — "
                "you MUST call browser_take_screenshot (real pixels) and reason from the image, "
                "not the DOM. For those visual tasks, re-screenshot after each navigation so "
                "you're looking at the CURRENT view. Use vision when it's called for; otherwise "
                "stay in DOM mode.\n\n"
                "USER REQUEST: " + task)
        env = dict(os.environ)
        env["OPERATOR_BOT"] = self.bot or ""   # action-tap stamps the right bot
        env["PATH"] = (os.path.expanduser("~/.local/bin") + ":"
                       + os.path.expanduser("~/.nvm/versions/node/v20.20.2/bin")
                       + ":" + env.get("PATH", ""))
        resume_id = self._session_ids.get(self.bot or "")

        if self._runtime == "codex":
            # codex exec: headless, JSONL events, ChatGPT-sub token (no API key).
            # Its ~/.codex/config.toml already wires the playwright MCP, so we just
            # exec it. `codex exec resume <thread_id>` threads context.
            env["CODEX_HOME"] = b["config_dir"]
            prompt = (b["persona"] + "\n\nTask: " + task)
            cmd = [binpath, "exec", "--json", "--skip-git-repo-check",
                   "--dangerously-bypass-approvals-and-sandbox"]
            if self.model:
                cmd += ["-m", self.model]
            if self.effort:
                cmd += ["-c", "model_reasoning_effort=" + json.dumps(self.effort)]
            if resume_id:
                cmd += ["resume", resume_id, prompt]
            else:
                cmd += [prompt]
        else:
            # claude -p: stream-json, Max/Pro OAuth in the bot's config dir.
            cfg_path = os.path.join(os.path.expanduser("~/.cache/computer-use"),
                                    f"operator-mcp-{self.bot}.json")
            mcp_cfg = {"mcpServers": {"playwright": {"command": "bash",
                       "args": [os.path.join(_BROWSE, "playwright-mcp.sh"), self.bot or ""]}}}
            try:
                os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
                with open(cfg_path, "w") as f:
                    json.dump(mcp_cfg, f)
            except OSError:
                pass
            env["CLAUDE_CONFIG_DIR"] = b["config_dir"]
            cmd = [binpath, "-p", task,
                   "--output-format", "stream-json", "--verbose",
                   "--permission-mode", "bypassPermissions",
                   # --settings/--strict-mcp-config both BREAK --resume (verified).
                   "--mcp-config", cfg_path,
                   "--append-system-prompt", b["persona"]]
            if resume_id:
                cmd += ["--resume", resume_id]
            if self.model:
                cmd += ["--model", self.model]
            if self.effort:
                cmd += ["--effort", self.effort]
        import tempfile as _tf
        _errf = _tf.TemporaryFile(mode="w+", encoding="utf-8")
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=b["cwd"], env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=_errf, text=True, bufsize=1)
            for line in self._proc.stdout:
                self._consume(line)
            self._proc.wait()
            if self._cur_session:
                self._session_ids[self.bot or ""] = self._cur_session
            # record the final answer in the shared transcript for cross-bot history
            final = next((m["text"] for m in reversed(self.messages)
                          if m.get("role") == "assistant" and m.get("text")), "")
            if final:
                self._transcript.append({"role": "assistant", "text": final[:1500]})
                self._transcript = self._transcript[-40:]
            self._last_bot = self.bot
            self._save_state()
            if self._proc.returncode == 0:
                self.state = "done"
            else:
                self.state = "error"
                # surface the specific failure reason from stderr (was discarded before)
                try:
                    _errf.seek(0); _tail = _errf.read().strip()
                    if _tail:
                        # keep the last meaningful lines, capped
                        _msg = "\n".join(_tail.splitlines()[-6:])[:400]
                        if not any(m.get("role") == "error" for m in self.messages[-3:]):
                            self.messages.append({"ts": time.time(), "role": "error",
                                                  "text": f"exit {self._proc.returncode}: {_msg}"})
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001
            self.state = "error"
            self.messages.append({"ts": time.time(), "role": "error", "text": str(e)})
        finally:
            try: _errf.close()
            except Exception: pass
            self.ended_ts = time.time()
            self._proc = None

    def _consume(self, line: str) -> None:
        """Parse one stream-json line → push assistant text into messages."""
        line = line.strip()
        if not line:
            return
        try:
            evt = json.loads(line)
        except Exception:
            return
        if getattr(self, "_runtime", "claude") == "codex":
            self._consume_codex(evt)
            return
        # capture the session id (for --resume continuity on the next turn)
        if evt.get("type") == "system" and evt.get("subtype") == "init":
            sid = evt.get("session_id")
            if sid:
                self._cur_session = sid
        # stream-json shape: {type:"assistant", message:{content:[{type:text|tool_use,...}]}}
        if evt.get("type") == "assistant":
            msg = evt.get("message") or {}
            for block in (msg.get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    t = (block.get("text") or "").strip()
                    if t:
                        self.messages.append({"ts": time.time(), "role": "assistant", "text": t})
                elif block.get("type") == "tool_use":
                    # surface browser actions inline so the trace interleaves
                    # thinking with actions (Operator-style).
                    name = block.get("name") or ""
                    label, detail = _action_label(name, block.get("input") or {})
                    if label:
                        self.messages.append({"ts": time.time(), "role": "action",
                                              "text": label, "detail": detail})
        elif evt.get("type") == "result":
            res = (evt.get("result") or "").strip()
            # the final assistant turn is usually re-sent as the result — don't
            # append a duplicate (it made the operator show "Worked for Ns" twice).
            last = self.messages[-1]["text"].strip() if self.messages else ""
            if res and res != last:
                self.messages.append({"ts": time.time(), "role": "assistant", "text": res})

    def _consume_codex(self, evt: dict) -> None:
        """Parse one codex `exec --json` JSONL event into messages."""
        t = evt.get("type")
        if t == "thread.started":
            tid = evt.get("thread_id")
            if tid:
                self._cur_session = tid          # codex resume id
        elif t == "item.completed":
            item = evt.get("item") or {}
            it = item.get("type")
            if it == "agent_message":
                txt = (item.get("text") or "").strip()
                if txt:
                    self.messages.append({"ts": time.time(), "role": "assistant", "text": txt})
            elif it in ("mcp_tool_call", "tool_call", "function_call"):
                # surface browser actions inline (Operator-style trace)
                name = item.get("tool") or item.get("name") or ""
                args = item.get("arguments") or item.get("input") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                label, detail = _action_label(name, args if isinstance(args, dict) else {})
                if label:
                    self.messages.append({"ts": time.time(), "role": "action",
                                          "text": label, "detail": detail})
            elif it == "command_execution":
                cmd = (item.get("command") or "").strip()
                if cmd:
                    self.messages.append({"ts": time.time(), "role": "action",
                                          "text": "Running command", "detail": cmd[:70]})
        elif t == "error":
            msg = (evt.get("message") or evt.get("error") or "").strip()
            if msg:
                self.messages.append({"ts": time.time(), "role": "error", "text": msg[:200]})

    def reset_session(self, bot: str = "") -> dict:
        """Forget stored session id(s) + the shared transcript so the next task
        starts a fresh conversation (wired to the operator's clear/trash button)."""
        if bot:
            self._session_ids.pop(bot, None)
        else:
            self._session_ids.clear()
        self._transcript = []
        self._last_bot = None
        self._save_state()
        return {"ok": True}

    def stop(self) -> dict:
        p = self._proc
        if p and self.is_running():
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
            self.state = "idle"
            return {"ok": True}
        return {"ok": False, "error": "nothing running"}

    def snapshot(self, since_ts: float = 0.0) -> dict:
        # `final` = the last assistant text of THIS turn, unfiltered by since_ts, so
        # the client can always render the reply bubble even if it missed the
        # incremental message or the agent emitted it right at turn-end (codex/gpt
        # sometimes flushes the final agent_message together with turn completion).
        final = next((m["text"] for m in reversed(self.messages)
                      if m.get("role") == "assistant" and m.get("text")), "")
        return {
            "bot": self.bot, "task": self.task, "state": self.state,
            "started_ts": self.started_ts, "ended_ts": self.ended_ts,
            "messages": [m for m in self.messages if m["ts"] > since_ts],
            "final": final,
        }


runner = AgentRunner()
