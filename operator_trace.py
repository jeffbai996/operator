"""Trace labeling for the operator cockpit — pure functions, no runner state.

Turns raw agent tool events into the human-readable action trace ("Clicking
(420, 315)", "Searching (\"terms\")") and cleans agy/gemma final text for the
chat pane. Extracted from operator_agent.py (1.0.8 R1) so the runner file
holds only the state machine.
"""
from __future__ import annotations

import os
import re as _re


def shot_dirs() -> list[str]:
    """Dirs whose files may be served BY BASENAME via /operator/shot/<name>.

    THE single source of truth (1.0.8 R3): the trace rewriter below
    (clean_gemma_text) and operator_view's shot route must agree on this
    list, or a rewritten inline-image link 404s. Env read at call time:
    the MCP screenshot output dir, plus each bot's session cwd (gpt/codex
    save screenshots there rather than in the MCP output dir).
    """
    return [os.path.realpath(os.path.expanduser(
        os.environ.get("COMPUTER_USE_OUTPUT_DIR")
        or os.environ.get("PLAYWRIGHT_OUTPUT_DIR")
        or "~/.cache/computer-use"))] + [
        os.path.realpath(os.path.expanduser("~/.operator-sessions/" + b))
        for b in ("claude-a", "claude-b", "gpt", "gemma")]


# Map a Playwright MCP tool call -> ("present-tense action label", "short detail")
# so the operator trace can interleave actions with the agent's thinking.
_ACTION_LABELS = {
    "browser_click": "Clicking", "browser_double_click": "Double-clicking",
    "browser_mouse_click_xy": "Clicking", "browser_mouse_drag_xy": "Dragging",
    "browser_mouse_move_xy": "Moving", "browser_mouse_down": "Pressing", "browser_mouse_up": "Releasing",
    "browser_mouse_wheel": "Scrolling",
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


# the control MCP's `computer` tool multiplexes every desktop action behind one
# name — label by its `action` arg so the trace reads "Clicking (420, 315)"
# instead of a bare "Using computer" (which could mean anything).
_COMPUTER_ACTION_LABELS = {
    "screenshot": "Took screenshot", "left_click": "Clicking",
    "right_click": "Right-clicking", "middle_click": "Middle-clicking",
    "double_click": "Double-clicking", "triple_click": "Triple-clicking",
    "mouse_move": "Moving cursor", "left_click_drag": "Dragging",
    "left_mouse_down": "Pressing mouse", "left_mouse_up": "Releasing mouse",
    "type": "Typing", "key": "Pressing", "hold_key": "Holding key",
    "scroll": "Scrolling", "wait": "Waiting",
}


def _computer_label(args: dict) -> tuple[str, str]:
    """computer{action,...} -> (label, detail) mirroring the browser_* trace style:
    coords for pointer actions, the text/key for keyboard, direction for scroll."""
    act = str(args.get("action") or "").lower()
    label = _COMPUTER_ACTION_LABELS.get(act)
    if not label:                        # unknown/new action — still name it
        return ("Using computer", act.replace("_", " "))
    def _xy(v):
        return (f"({v[0]}, {v[1]})"
                if isinstance(v, (list, tuple)) and len(v) >= 2 else "")
    if act == "left_click_drag":
        d = (_xy(args.get("start_coordinate")) + " → " + _xy(args.get("coordinate"))).strip(" →")
    elif act in ("type", "key", "hold_key"):
        d = str(args.get("text") or "")[:120]
        if act in ("key", "hold_key"):
            # trace cosmetics: the Windows key shows as its logo, not "win"
            d = _re.sub(r"\bwin\b", "⊞", d)
    elif act == "scroll":
        d = (str(args.get("scroll_direction") or "") + " " + _xy(args.get("coordinate"))).strip()
    elif act == "wait":
        dur = args.get("duration")
        d = f"{dur:g}s" if isinstance(dur, (int, float)) else ""
    else:
        d = _xy(args.get("coordinate"))
    return label, scrub_detail(d)


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
    # memory / recall (host-app, search)
    "recall": "Recalling", "memory": "Checking data", "search": "Searching",
    "get_corpus": "Searching", "list_corpora": "Checking data",
    # markets (tool)
    "get_quote": "Checking data", "quote": "Checking data",
    "get_positions": "Checking data", "tool_quote": "Checking data",
    "tool_get_positions": "Checking data", "tool_get_account_summary": "Checking data",
    "tool_margin": "Checking data", "tool_get_historical_bars": "Pulling data",
    # docs / misc
    "query-docs": "Reading docs", "resolve-library-id": "Looking up library",
    "task": "Delegating", "todowrite": "Updating todos", "webfetch_url": "Fetching",
    "fill_form": "Filling form",
    # agy / Antigravity CLI tool names (gemma driver) — give them real verbs so the
    # trace reads like the Claude/codex trace instead of "Using `grep_search`".
    "grep_search": "Searching", "codebase_search": "Searching code",
    "find_files": "Finding files", "view_file": "Reading file",
    "read_file": "Reading file", "view_code_item": "Reading code",
    "run_command": "Running command", "run_terminal_command": "Running command",
    "read_url_content": "Fetching", "search_web": "Searching web",
    "write_to_file": "Writing file", "edit_file": "Editing file",
    "replace_file_content": "Editing file", "list_dir": "Listing files",
    "create_memory": "Saving memory", "browser_preview": "Opening preview",
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


def gerund_label(bare: str) -> str:
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


def mcp_resource_label(name: str) -> str:
    """Map generic MCP resource/listing ops to a clean verb . Returns '' if nothing fits."""
    n = (name or "").lower().rsplit("__", 1)[-1]
    if any(k in n for k in ("list_resources", "listresources", "resources/list", "list_dir",
                            "listdir", "list_directory", "readdir")):
        return "Listing resources"
    if any(k in n for k in ("read_resource", "readresource", "get_resource", "resources/read")):
        return "Reading resource"
    if "list" in n:
        return "Listing"
    return ""


def scrub_detail(d: str) -> str:
    """Sanitize an action detail before it renders in the (possibly public) trace.
    Strips absolute home paths to a basename and removes a leading user home prefix,
    so a gemma schema-read can't leak '/home/<user>/.gemini/...'. Idempotent."""
    if not isinstance(d, str) or not d.strip():
        return ""
    d = d.strip()
    # a detail that's PURELY an absolute path → show just the last path component
    if _re.fullmatch(r"/(?:home|Users|root)/[^\s]+", d):
        d = d.rstrip("/").rsplit("/", 1)[-1] or d
    else:
        # a home path embedded in a sentence → collapse to ~/<tail-basename>
        d = _re.sub(r"/(?:home|Users)/[^/\s]+(/[^\s]*)?",
                    lambda m: "~/" + (m.group(1).rstrip("/").rsplit("/", 1)[-1] if m.group(1) else ""),
                    d)
    return d[:120]


def action_label(tool: str, args: dict) -> tuple[str, str]:
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
        # desktop control MCP (computer / perceive / game_macro): action-aware
        # labels so a desktop trace reads as cleanly as a browser one.
        if low == "computer":
            return _computer_label(args if isinstance(args, dict) else {})
        if low == "perceive":
            d = str(args.get("map") or "") if isinstance(args, dict) else ""
            return "Reading the screen", scrub_detail(d)
        if low == "game_macro":
            d = ""
            if isinstance(args, dict):
                ops = args.get("ops")
                d = f"{len(ops)} steps" if isinstance(ops, list) and ops else ""
                m = str(args.get("map") or "")
                if m:
                    d = (d + " · " + m).strip(" ·")
            return "Running macro", d
        nb = _NONBROWSER_LABELS.get(low)
        if nb is None:
            # try to generalize to present-continuous ("fetch_messages" -> "Fetching
            # messages"); if the first token isn't a known verb, fall back to the
            # code-block form ("Using `tool_name`") the trace renders as a code chip.
            nb = gerund_label(bare)
            if not nb:
                nb = ("Using `" + bare + "`") if bare else ""
        if nb:
            d = ""
            if isinstance(args, dict):
                # Preferred fields, in priority order. agy/Antigravity uses CapitalCase
                # (Query, SearchPath, AbsolutePath, CommandLine, Url, ...) where Claude/
                # codex use lowercase — so we match case-INSENSITIVELY to surface the
                # arg for BOTH. This is what makes gemma's trace read "Searching
                # (\"terms\")" instead of a bare "Searching web".
                _pref = ("query", "q", "searchterm", "url", "commandline", "command",
                         "cmd", "pattern", "prompt", "symbol", "text", "name",
                         "absolutepath", "targetfile", "filepath", "file_path",
                         "directorypath", "path", "searchpath", "id")
                _lc = {k.lower(): v for k, v in args.items()
                       if isinstance(v, str) and v.strip()}
                for k in _pref:
                    if k in _lc:
                        d = _lc[k].strip()[:120]; break
            return nb, scrub_detail(d)
        return "", ""
    label = _ACTION_LABELS.get(bare, bare.replace("browser_", "").replace("_", " ").capitalize())
    detail = ""
    if isinstance(args, dict):
        # CASE-INSENSITIVE arg access: agy/Gemini sends CapitalCase keys
        # (Text/Url/Key/Value/Selector/X/Y/...) where claude/codex send lowercase.
        # The browser detail path used to read lowercase-only, so EVERY agy browser
        # action with a CapitalCase arg surfaced no detail (the systematic gap behind
        # bare "Typing"/"Browsing"/"Pressing"). Mirror args lowercased and look up
        # through _ci so both spellings hit. (Also unwraps a nested Arguments dict that
        # some agy steps leave wrapped.)
        _src = args
        if isinstance(args.get("Arguments"), dict):   # belt-and-suspenders unwrap
            _src = {**args, **args["Arguments"]}
        _ci = {}
        for _k, _v in _src.items():
            if isinstance(_k, str):
                _ci.setdefault(_k.lower(), _v)
        def _g(*keys):
            for _k in keys:
                if _k in _src: return _src[_k]
                if _k.lower() in _ci: return _ci[_k.lower()]
            return None
        # coordinate-mouse tools (browser_mouse_*_xy): surface the click/drag coords
        # so the trace shows WHERE it clicked, e.g. "Clicking (420, 315)" or a drag
        # "(120, 80) → (300, 240)". Tolerant of common key spellings.
        if "_xy" in bare or bare in ("browser_mouse_down", "browser_mouse_up", "browser_mouse_move"):
            def _num(*keys):
                for k in keys:
                    v = _g(k)
                    if isinstance(v, (int, float)):
                        return int(round(v))
                return None
            x = _num("x", "startX", "fromX", "x1"); y = _num("y", "startY", "fromY", "y1")
            x2 = _num("endX", "toX", "x2"); y2 = _num("endY", "toY", "y2")
            if x is not None and y is not None:
                detail = f"({x}, {y})"
                if x2 is not None and y2 is not None:
                    detail += f" → ({x2}, {y2})"
        if not detail:
            # agy attaches a human one-liner per call ("Clicking learn more link") —
            # prefer it; it beats a raw selector. Then a HUMAN description
            # (element/text), THEN a selector — but DROP a value that's just an opaque
            # ref (e6, s3) or a bare tag ("a", "div"): those mean nothing to the viewer,
            # better to show the bare verb ("Clicking") than "Clicking — a".
            def _trivial(val):
                v = val.strip()
                return (_re.fullmatch(r"[a-z]?\d+|e\d+|s\d+|f\d+", v)         # auto-ref e6/s3
                        or _re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]{0,2}", v))        # bare tag a/div
            _act = (_g("toolAction") or _g("toolSummary") or "")
            if isinstance(_act, str) and _act.strip():
                _d = _act.strip()
                # drop a leading word that just re-states the label's verb so we don't
                # render "Clicking — Clicking learn more link" / "Took screenshot —
                # Taking screenshot ...". Covers present + past forms of common verbs.
                _roots = ("click", "tap", "typ", "screenshot", "screen shot", "navigat",
                          "read", "view", "scroll", "drag", "select", "press", "hover",
                          "tak", "took", "go", "open", "search")
                _w = _d.split(None, 1)
                if len(_w) == 2 and any(_w[0].lower().startswith(r) for r in _roots):
                    _d = _w[1]
                    _w2 = _d.split(None, 1)   # trim a left-behind article/prep
                    if len(_w2) == 2 and _w2[0].lower() in ("the", "a", "an", "on", "of", "to"):
                        _d = _w2[1]
                detail = _d[:120]
            if not detail:
                for k in ("element", "text", "value", "url", "key", "selector", "target", "query"):
                    v = _g(k)
                    if isinstance(v, str) and v.strip() and not _trivial(v):
                        detail = v.strip()[:120]
                        break
            if not detail:
                rv = _g("ref")
                if isinstance(rv, str) and rv.strip() and not _trivial(rv):
                    detail = rv.strip()[:120]
        if not detail and (_g("width") is not None or _g("height") is not None):  # screenshot → resolution
            w, h = _g("width"), _g("height")
            if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                detail = f"{int(w)}×{int(h)}"
        if not detail:                      # Waiting: surface the time, humanized
            for k in ("time", "timeout", "seconds", "ms"):
                v = _g(k)
                if isinstance(v, (int, float)):
                    secs = v / 1000.0 if k == "ms" else float(v)
                    detail = fmt_duration(secs)
                    break
    return label, scrub_detail(detail)


# #4 handoff marker the agent emits when it hits a human-only gate (captcha/2FA/etc).
# Tolerant: optional spaces, case-insensitive key, reason optional.
_TAKE_CONTROL_RE = _re.compile(r"\[\[\s*TAKE[_ ]?CONTROL\s*:?\s*(.*?)\s*\]\]",
                               _re.IGNORECASE | _re.DOTALL)


def clean_gemma_text(text: str) -> str:
    """agy/gemma final output carries CLI-runner noise that renders badly in the chat:
      - "🛑 Task started: ..." status-bullet lines (agy's own progress echo)
      - ![alt](file:///...) image markdown pointing at a LOCAL path (a web page can't
        load file://, so it renders as a broken image) — keep the alt as a plain note
      - a trailing files=[...] literal (agy echoing its attachment list)
      - +----+ ASCII tables that render as a mangled blob unless monospaced
    Strip/normalize these so the reply reads clean. Best-effort; never raises."""
    if not isinstance(text, str) or not text.strip():
        return text or ""
    t = text
    # HARMONY-FORMAT tokens (gpt-oss-120b via agy emits OpenAI "harmony" reasoning
    # markup that leaks raw into the trace as malformed text): strip the control tokens
    # <|start|> <|end|> <|message|> <|channel|> <|constrain|> <|return|> and the bare
    # channel headers (analysis/commentary/final) that precede a message. No-op for
    # models that don't use harmony (Gemini/Claude via agy).
    if '<|' in t and ('channel' in t or 'message' in t):
        # Parse harmony: ...<|channel|>NAME<|message|>BODY<|end|>... Keep ONLY the
        # `final` channel's body (the real answer); drop analysis/commentary reasoning
        # and all control tokens. If no `final` channel, fall back to stripping tokens.
        _finals = _re.findall(r'<\|channel\|>\s*final\s*<\|message\|>(.*?)(?:<\|end\|>|<\|start\|>|<\|return\|>|$)', t, _re.S)
        if _finals:
            t = '\n'.join(x.strip() for x in _finals if x.strip())
        else:
            t = _re.sub(r'<\|channel\|>\s*(?:analysis|commentary)\s*<\|message\|>.*?(?=<\||$)', '', t, flags=_re.S)
            t = _re.sub(r'<\|[a-z_]+\|>', '', t)
            t = _re.sub(r'(?m)^\s*(?:analysis|commentary|final|assistant)\s*$', '', t)
    # strip Gemini's native thinking-block header line ("**Thought for Ns:**" or
    # "Thinking with [effort] effort...") — agy injects these at the top of the
    # PLANNER_RESPONSE thinking field; they duplicate our narrator's own indicator.
    t = _re.sub(r'(?m)^\s*\*?\*?(?:Thought for \d+s?:?|Thinking with \[[^\]]+\] effort\.{0,3})\*?\*?\s*\n?', '', t, count=1)
    # drop "🛑 Task started:" (and bare "Task started:") progress lines
    t = _re.sub(r'(?m)^\s*(?:🛑|🟢|▶️?)?\s*Task started:.*$', '', t)
    # screenshot references -> rewrite to the cockpit's /operator/shot route so
    # they render INLINE (file:// and absolute paths can't load in a browser).
    # Agents phrase these several ways — claude emits ![alt](file:///...), gpt
    # emits a PLAIN [name.jpeg](/abs/path.jpeg) link into its session cwd — so
    # match image-suffixed file://+absolute paths in BOTH image and plain link
    # form. Servable dirs mirror the route's whitelist (output dir + per-bot
    # session dirs); basename-match into them, else fall back to a text note.
    def _servable(base):
        return (base and os.path.splitext(base)[1].lower()
                in ('.png', '.jpg', '.jpeg', '.webp')
                and any(os.path.isfile(os.path.join(d, base))
                        for d in shot_dirs()))

    def _shot_sub(m):
        alt, path = m.group(1), m.group(2)
        base = os.path.basename(_re.sub(r'^file://', '', path))
        if _servable(base):
            return '![%s](operator/shot/%s)' % (alt or 'screenshot', base)
        return 'took a screenshot'

    # image form: ![alt](file:///... | /abs/...)
    t = _re.sub(r'!\[([^\]]*)\]\((file://[^)]*|/[^)]*)\)', _shot_sub, t)
    # plain-link form pointing at an image file -> promote to an inline image
    t = _re.sub(r'(?<!!)\[([^\]]*)\]\(((?:file://)?/[^)]+\.(?:png|jpe?g|webp))\)',
                _shot_sub, t, flags=_re.IGNORECASE)
    # any other ![](non-http, non-route) image -> drop it (keep nothing; non-renderable)
    t = _re.sub(r'!\[[^\]]*\]\((?!https?://|/?operator/shot/)[^)]*\)', '', t)
    # [label](file:///...) plain (non-image) link -> a browser can't load file:// either;
    # keep just the label text so a self-narrated checklist ("see [trace.json](file:///...)")
    # doesn't leave a dead link in the reply .
    t = _re.sub(r'(?<!!)\[([^\]]*)\]\(file://[^)]*\)', lambda m: m.group(1) or '', t)
    # strip a trailing files=[...] literal (single or multi-line)
    t = _re.sub(r'(?ms)^\s*files\s*=\s*\[.*?\]\s*$', '', t)
    # wrap contiguous +---+ / | ascii-table blocks in a fenced code block so they align
    lines = t.split('\n')
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        is_tbl = bool(_re.match(r'^\s*[+|]', ln)) and ('+' in ln or '|' in ln)
        if is_tbl:
            block = []
            while i < len(lines) and _re.match(r'^\s*[+|]', lines[i]) and ('+' in lines[i] or '|' in lines[i]):
                block.append(lines[i]); i += 1
            if len(block) >= 2:
                out.append('```'); out.extend(block); out.append('```')
            else:
                out.extend(block)
            continue
        out.append(ln); i += 1
    t = '\n'.join(out)
    # collapse 3+ blank lines left by the strips
    t = _re.sub(r'\n{3,}', '\n\n', t).strip()
    return t


def extract_handoff(text: str) -> tuple[str, str | None]:
    """Strip any [[TAKE_CONTROL: reason]] marker from assistant text.
    Returns (clean_text, reason_or_None). reason is '' if the marker had no text."""
    if not text or "[[" not in text:
        return text, None
    m = _TAKE_CONTROL_RE.search(text)
    if not m:
        return text, None
    reason = (m.group(1) or "").strip()
    clean = _TAKE_CONTROL_RE.sub("", text).strip()
    return clean, reason


def fmt_duration(secs: float) -> str:
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
