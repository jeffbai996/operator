"""agy / Antigravity trajectory subsystem for the operator (1.0.9 R5).

agy's `-p` print mode emits NO event stream — only the final plain text — so
the live thinking/action trace is reverse-engineered from the trajectory file
(transcript_full.jsonl) its brain dir writes incrementally, and the resume id
from a set-difference over its conversations dir. All of that quarantine lives
here; operator_agent keeps only thin hooks.

parse_trajectory(path, r) streams messages into a runner-shaped sink `r` —
the narrow interface it touches (see AgentRunner, which implements it):
  r.messages (list)  r.handoff (dict|None)  r._touch()  r._note_action(n, a)
  r._agy_seen (set)  r._agy_noprogress_streak (int)  r._agy_loop_warned (bool)
  r._agy_loop_nudge_pending (bool)
"""
from __future__ import annotations

import json
import os
import time

from operator_trace import (action_label, clean_gemma_text, extract_handoff,
                            gerund_label, mcp_resource_label)

# overthink-loop guard: a PLANNER_RESPONSE step with no tool_calls and no final
# `content` is pure scratch reasoning (agy "thinking out loud" without acting).
# A long unbroken run of these is the stuck-in-a-loop pattern (#40, e.g. Flash
# 3.5 re-describing a PDF instead of scrolling it). Warn once when the streak
# crosses this; never auto-kill the run .
LOOP_WARN_STREAK = 6   # consecutive no-progress planner steps


def filter_stop_noise(stdout_text: str) -> str:
    """agy prints interrupt/timeout noise to stdout when terminated (user hit
    Stop) — e.g. "Error: timed out waiting for response". Strip those lines so
    they never surface as a reply; may return ''."""
    _NOISE = ("timed out waiting for response", "timed out waiting",
              "request was aborted", "operation was canceled", "operation was cancelled")
    kept = [ln for ln in stdout_text.splitlines()
            if not any(n in ln.lower() for n in _NOISE)]
    return "\n".join(kept).strip()


# agy conversation-id capture. agy's -p print mode emits no session id, but
# every run creates <uuid>.db in its conversations dir — so we set-difference
# the dir across the run and thread the new id back via --conversation on the
# next turn. Ambiguity (0 or 2+ new ids — e.g. the owner running agy in a terminal
# concurrently) yields None: resuming the WRONG conversation is far worse than
# running fresh (the shared-transcript inject still carries continuity).
CONV_DIR = os.path.expanduser("~/.gemini/antigravity-cli/conversations")


def conversation_ids(conv_dir: str = CONV_DIR) -> set:
    """Stems of the conversation .db files (the ids). Empty set if unreadable."""
    try:
        return {f[:-3] for f in os.listdir(conv_dir) if f.endswith(".db")}
    except OSError:
        return set()


def new_conversation(before: set, after: set) -> str | None:
    """The one id created during the run, or None when it can't be known safely."""
    new = after - before
    return next(iter(new)) if len(new) == 1 else None


def snapshot_trajectories(brain_dir: str) -> dict:
    """Map {transcript_full.jsonl path -> mtime} under the agy brain dir, taken
    BEFORE launch so we can identify THIS run's trajectory afterward (the one
    that's new or freshest-modified since)."""
    out: dict = {}
    bd = brain_dir
    if not bd or not os.path.isdir(bd):
        return out
    try:
        for conv in os.scandir(bd):
            if not conv.is_dir():
                continue
            tp = os.path.join(conv.path, ".system_generated", "logs",
                              "transcript_full.jsonl")
            try:
                out[tp] = os.path.getmtime(tp)
            except OSError:
                pass
    except OSError:
        pass
    return out


def snapshot_offsets(trajectories: dict) -> dict:
    """Map each existing trajectory to its pre-launch byte length.

    A resumed agy conversation appends to the same JSONL. Starting reads at
    this offset skips prior turns without scanning hundreds of MB of old brain
    files or trusting every legacy transcript to decode cleanly.
    """
    offsets = {}
    for path in (trajectories or {}):
        try:
            offsets[path] = os.path.getsize(path)
        except OSError:
            continue
    return offsets


def find_trajectory(brain_dir: str, before: dict, strict: bool = False,
                    allow_touched: bool = False) -> str | None:
    """Pick THIS run's transcript_full.jsonl: a path that's NEW since the pre-launch
    snapshot, or one whose mtime advanced. Falls back to the globally-freshest if
    nothing looks new (best-effort — never raises).

    strict=True (the LIVE poll): normally return ONLY a brand-new path. A known
    resumed conversation instead passes allow_touched=True because agy appends to
    its existing transcript; the pre-launch byte offset prevents old-turn replay.
    The post-run _flush_agy calls non-strict so it can still fall back."""
    bd = brain_dir
    if not bd or not os.path.isdir(bd):
        return None
    before = before or {}
    now = snapshot_trajectories(bd)
    # PREFER a path that did NOT exist before this run — that is unambiguously
    # THIS run's trajectory. A pre-existing path whose mtime merely advanced is a
    # trap: a prior run's brain dir can get touched and win the freshest-changed
    # race, so the live-poll locks onto STALE steps (you'd see a previous task's
    # thinking/actions replayed).
    brand_new = [(m, pth) for pth, m in now.items() if pth not in before]
    if brand_new:
        return max(brand_new)[1]
    touched = [(m, pth) for pth, m in now.items() if m > before.get(pth, 0)]
    if touched:
        if not strict or allow_touched:
            return max(touched)[1]
    if strict:
        return None                        # live poll waits for the real new file
    # non-strict (final flush): fall back to the freshest overall.
    if now:                                # nothing new/touched — freshest overall
        return max((m, pth) for pth, m in now.items())[1]
    return None


def parse_trajectory(path: str, r) -> bool:
    """Parse agy's structured trajectory (transcript_full.jsonl) into ordered
    thinking/action/answer messages — full parity with the codex/claude trace.

    VERIFIED step shapes (real tool-using run, agy 1.0.13):
      - PLANNER_RESPONSE (source MODEL): the interesting one. Carries
        `thinking` (str reasoning) AND `tool_calls` (list of {name, args}) on the
        PLANNING step, and `content` (str final answer) on the FINAL step.
      - RUN_COMMAND / other MODEL-source non-PLANNER types: a discrete tool/action
        step (content = a result log). We surface the tool_calls from the planner
        steps as the actions (they carry the real tool name + args); a MODEL-source
        non-planner step with no matching planner tool_call is surfaced generically.
      - USER_INPUT / CONVERSATION_HISTORY / CHECKPOINT (SYSTEM): skip.

    Returns True if it parsed at least one assistant message (so the caller knows
    the trajectory carried the answer and can skip the stdout fallback). Best-effort:
    any error → return False and let the caller fall back to plain stdout."""
    try:
        steps = []
        offset = (getattr(r, "_agy_offsets", {}) or {}).get(path, 0)
        with open(path, "rb") as f:
            if offset:
                try:
                    f.seek(offset)
                except OSError:
                    f.seek(0)
            for line in f:
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    steps.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return False
    steps.sort(key=lambda s: s.get("step_index", 0))   # thinking→action→answer order
    # The PLANNER_RESPONSE.tool_calls are the AUTHORITATIVE action list (clean tool
    # name + args + a built-in human label). The standalone tool steps (RUN_COMMAND
    # etc.) are just execution echoes of those same calls, so if ANY planner carries
    # tool_calls we drive actions from the planners and SUPPRESS the echo steps
    # (avoids the duplicate "Running command"). Only if NO planner had tool_calls do
    # we fall back to surfacing the standalone MODEL non-planner steps as actions —
    # that path also covers a future agy where browser/MCP calls appear ONLY as their
    # own step type and never as planner tool_calls.
    any_planner_tools = any(
        o.get("source") == "MODEL" and o.get("type") == "PLANNER_RESPONSE"
        and o.get("tool_calls") for o in steps)
    got_answer = False
    for o in steps:
        if o.get("source") != "MODEL":
            continue                       # skip USER_INPUT / CONVERSATION_HISTORY / CHECKPOINT
        _sidx = (path, o.get("step_index", id(o)))   # qualify by file: step_index
        if _sidx in r._agy_seen:                       # collides across trajectories
            continue                       # already emitted on a prior (live) parse
        r._agy_seen.add(_sidx)
        r._touch()   # a NEW trajectory step = progress (agy has no stdout stream)
        typ = o.get("type")
        if typ == "PLANNER_RESPONSE":
            think = o.get("thinking")
            if isinstance(think, str) and think.strip():
                _ck = clean_gemma_text(think.strip())
                if _ck:
                    # role="thinking", NOT "assistant": this is scratch reasoning, not
                    # a final answer. snapshot()'s `final` picker and the client's
                    # reply-bubble logic both key off role=="assistant", so tagging it
                    # separately keeps it showing live in the trace (the client still
                    # needs a branch for this role) while making it structurally
                    # impossible for raw thinking/work-summary text — including any
                    # checklist + file:// links — to become the user-visible reply if
                    # the turn ends (or is cut off mid-loop) before a real `content`
                    # answer ever arrives .
                    r.messages.append({"ts": time.time(), "role": "thinking",
                                          "text": _ck})
            _had_tool_calls = bool(o.get("tool_calls"))
            # tool_calls: list of {name, args} — same shape action_label wants.
            for tc in (o.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name") or ""
                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                # UNWRAP agy's meta-tools (esp. Gemini Flash): it wraps every real
                # MCP call in `call_mcp_tool` with the actual tool in args["ToolName"]
                # and the real args in args["Arguments"] — so action_label saw only
                # "call_mcp_tool" and rendered "Calling MCP tool". Reach through to
                # the real tool/args so browser_* maps to clean verbs + emojis.
                if name in ("call_mcp_tool", "callMcpTool", "mcp_tool", "run_mcp_tool"):
                    _inner = (args.get("ToolName") or args.get("toolName")
                              or args.get("tool") or args.get("name") or "")
                    _ia = args.get("Arguments") or args.get("arguments") or args.get("args")
                    if isinstance(_ia, str):
                        try: _ia = json.loads(_ia)
                        except Exception: _ia = {}
                    if _inner:
                        # keep agy's toolAction/Summary on the args as a label fallback
                        if isinstance(_ia, dict):
                            _ia.setdefault("toolAction", args.get("toolAction", ""))
                            _ia.setdefault("toolSummary", args.get("toolSummary", ""))
                        name, args = _inner, (_ia if isinstance(_ia, dict) else {})
                elif name == "view_file":
                    name = "browser_get_text"  # maps to "Reading"; detail=path below
                    args = {"path": (tc.get("args") or {}).get("AbsolutePath", ""),
                            "toolAction": (tc.get("args") or {}).get("toolAction", ""),
                            "toolSummary": (tc.get("args") or {}).get("toolSummary", "")}
                r._note_action(name, args)
                label, detail = action_label(name, args)
                if not label:
                    # Our mapper didn't recognize it. Prefer OUR gerund verb over
                    # agy's built-in toolAction when that's the generic "Calling
                    # (MCP) tool" noise — only fall back to agy's label if it's a
                    # SPECIFIC one (e.g. "Read file", "Search web"). Last resort:
                    # a clean "Using tool" (never the raw tool name / "calling mcp server").
                    _agy_lbl = (args.get("toolAction") or args.get("toolSummary") or "").strip()
                    _generic = (not _agy_lbl) or _agy_lbl.lower() in (
                        "calling mcp tool", "calling tool", "running tool",
                        "using tool", "tool call", "mcp tool")
                    label = (gerund_label(name) or (_agy_lbl if not _generic else "")
                             or mcp_resource_label(name) or "Using tool")
                if label and not detail:
                    # agy attaches a human description per call (toolAction /
                    # toolSummary, e.g. "Clicking learn more link") — PREFER that as
                    # the detail; it's cleaner than a raw selector. Then fall back to
                    # the real arg (target/selector/url/...). If NOTHING is present we
                    # leave detail empty so the trace shows the bare verb ("Clicking")
                    # rather than an opaque "element".
                    # token roots already implied by common labels, so a toolAction
                    # echoing the same verb ("Clicking ..."/"Took screenshot" vs
                    # "Taking screenshot ...") doesn't render "Clicking — Clicking ...".
                    _verb_roots = {"click", "tap", "typ", "screenshot", "navigat",
                                   "read", "scroll", "drag", "select", "press", "hover"}
                    for k in ("toolAction", "toolSummary", "CommandLine", "command",
                              "url", "query", "text", "target", "selector"):
                        v = args.get(k)
                        if not (isinstance(v, str) and v.strip()):
                            continue
                        _d = v.strip()
                        if k in ("toolAction", "toolSummary"):
                            # drop a leading word that just re-states the label's verb
                            _w = _d.split(None, 1)
                            if len(_w) == 2 and any(_w[0].lower().startswith(r) for r in _verb_roots):
                                _d = _w[1]
                            _w2 = _d.split(None, 1)   # then a left-behind article/prep
                            if len(_w2) == 2 and _w2[0].lower() in ("the", "a", "an", "on", "of"):
                                _d = _w2[1]
                        elif k in ("target", "selector"):
                            # a bare tag selector ("a", "div", "button") is useless as a
                            # label — skip it so the trace shows the bare verb instead.
                            import re as _re2
                            if _re2.fullmatch(r"[a-zA-Z][a-zA-Z0-9]{0,2}", _d):
                                continue
                        if _d:
                            detail = _d[:120]; break
                if label:
                    r.messages.append({"ts": time.time(), "role": "action",
                                          "text": label, "detail": detail})
            ans = o.get("content")
            _had_answer = False
            if isinstance(ans, str) and ans.strip():
                txt, _reason = extract_handoff(clean_gemma_text(ans.strip()))
                if _reason is not None and not r.handoff:
                    r.handoff = {"reason": _reason, "ts": time.time()}
                if txt:
                    r.messages.append({"ts": time.time(), "role": "assistant", "text": txt})
                    got_answer = True
                    _had_answer = True
            if _had_tool_calls or _had_answer:
                r._agy_noprogress_streak = 0
            else:
                r._agy_noprogress_streak += 1
                if (r._agy_noprogress_streak >= LOOP_WARN_STREAK
                        and not r._agy_loop_warned):
                    r._agy_loop_warned = True
                    r._agy_loop_nudge_pending = True  # #40b: nudge next turn
                    r.messages.append({"ts": time.time(), "role": "error",
                        "text": ("⚠️ This looks stuck in a loop — %d steps of reasoning "
                                  "in a row with no tool call or answer. Consider "
                                  "stopping if it doesn't recover."
                                  % r._agy_noprogress_streak)})
        elif not any_planner_tools:
            # No planner tool_calls in this run → surface standalone MODEL non-planner
            # steps (RUN_COMMAND, or a future browser/MCP step type) as actions. The
            # content here is a result LOG, not call args — take just a one-line snippet.
            content = o.get("content")
            snippet = ""
            if isinstance(content, str) and content.strip():
                snippet = content.strip().splitlines()[0][:120]
            label = ""
            if isinstance(typ, str):
                label = gerund_label(typ.lower()) or typ.replace("_", " ").capitalize()
            if label:
                r.messages.append({"ts": time.time(), "role": "action",
                                      "text": label, "detail": snippet})
    return got_answer
