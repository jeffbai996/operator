"""Run-completion pings — the cockpit's notification surface.

A finished run is invisible unless you happen to be looking at the cockpit
(the owner 2026-08-05: "Polished for 7m 13s" is a thing you found out by looking).
One message per terminal run, posted to a dedicated Discord alerts channel,
with the run's final frame attached when there is one.

Discord completion pings were rejected on 2026-07-01 in favour of the
nav-badge unseen counter. That call was reversed on 2026-08-05, once a
dedicated alerts channel existed: the objection was never the ping, it was
the ping landing in a channel somebody talks in.

Config (both required, absence = feature off):
    OPERATOR_PING_CHANNEL          Discord channel id to post into
    SQUAD_HELPER_DISCORD_TOKEN     the squad helper's bot token, or the same
                                   key in ~/.config/host-app/env

The channel id is env-only on purpose. Real channel/account identifiers do
not belong in source, and an unset variable is also the off switch.

Contract, inherited from the flight recorder: a notification failure must
NEVER touch the run. Everything here returns instead of raising, and the
send runs off the run thread so a slow Discord cannot stall a terminal
state transition.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger(__name__)

CHANNEL_ENV = "OPERATOR_PING_CHANNEL"
# Named HELPER_ENV, not TOKEN_ENV: the pre-commit secret guard reads
# `TOKEN_something = "<16+ chars>"` as a credential being committed, which is
# the right instinct and a false positive here — this holds the NAME of the
# variable, never its value. Don't rename it back.
HELPER_ENV = "SQUAD_HELPER_DISCORD_TOKEN"
_HELPER_FILE = "~/.config/host-app/env"

DISCORD_LIMIT = 2000
TASK_LIMIT = 300
# Rows are budgeted, not counted: keep the NEWEST steps that fit inside
# Discord's message ceiling and mark what was dropped, the way the agent view
# does (the owner 2026-08-06: "maximize the allowed height"). A fixed row count
# threw away room on a short trace and overflowed on a chatty one.
LIVE_STEPS_MAX = 40      # hard ceiling so one runaway trace can't own the card
LIVE_POLL_S = 3.0        # how often the watcher samples the runner
LIVE_EDIT_S = 4.0        # floor between edits — Discord rate-limits PATCH
# The spinner needs a heartbeat: a run that thinks for two minutes without a
# tool call would otherwise freeze mid-frame and read as wedged, which is the
# opposite of what a liveness cue is for. This deliberately costs edits on a
# quiet run (~4/min) — cheap against Discord's 5-per-5s, and the point of the
# glyph is that it moves.
LIVE_SPIN_S = 15.0
LIVE_MAX_S = 3 * 3600    # watcher self-terminates; never outlives a wedged run
_MAX_UPLOAD = 8 * 1024 * 1024        # comfortably under Discord's cap
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")

# The cockpit paints ✓ / ✕ / ⏹ on a finished run (1.0.30). The ping speaks the
# same three marks so the phone and the screen agree at a glance.
# The agent view's exact vocabulary (scripts/agent_view.py): a spinner while
# live, a solid ● when it settles, ✗ when it didn't. One squad, one language
# for "this is alive" and "this is finished" (the owner 2026-08-06).
GLYPHS = {"done": "●", "error": "✗", "interrupted": "⏹", "running": "◉"}
_SPINNER = ("◐", "◓", "◑", "◒")
_STEP_DONE = "●"
_STEP_LIVE = "◉"
# Surface reads faster as a picture than as the word "browser" three lines down.
_SURFACE_EMOJI = {"browser": "🌐", "desktop-sandbox": "📦", "desktop-real": "🖥️",
                  "sandbox": "📦", "computer": "🖥️"}
# What the surface is CALLED on the card. "browser" describes the mechanism;
# the thing that ran is Operator, and that is what you want to read at a glance
# in an alerts channel shared with everything else (the owner 2026-08-11).
_SURFACE_LABEL = {"browser": "operator"}
# Card width. See _clamp — this is the claude bot's number, not a new one.
LINE_MAX = 88
_HEADS = {"done": "Finished", "error": "Failed", "interrupted": "Stopped",
          "running": "Running"}


# ── formatting (pure) ───────────────────────────────────────────────────

def _fmt_duration(seconds: float) -> str:
    """Same shape the agent view prints — 9s / 4m46s / 1h02m. Two clocks side
    by side in one channel should not format time two different ways."""
    s = max(0, int(seconds or 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s" if s % 60 else f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _fmt_tokens(n: int | None) -> str:
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fence_safe(text: str) -> str:
    """A task or detail carrying ``` would close the block early and spill the
    rest of the message into the channel as prose (the runaway-fence bug from
    the digest work). Neutralise the sequence, keep the characters."""
    return (text or "").replace("```", "'''")


def _clamp(line: str) -> str:
    """Hold every row inside the card width.

    Same clamp the claude bot's tool trace uses (scripts/tool_watcher.py): 88
    chars plus the one-space marker below is ~89 cells, which is the card width
    the owner settled on 2026-06-25. A line past it wraps, and a wrapped line loses
    its marker prefix and renders as its own unstyled chunk — so overflow does
    not just look untidy, it breaks the colouring.
    """
    line = line or ""
    return line if len(line) <= LINE_MAX else line[: LINE_MAX - 1] + "…"


def _block(lines: list[str]) -> str:
    """A ```diff card. Callers pass each row already carrying its marker:
    a leading space renders plain, '+' green, '-' red. Plain is the default
    because a card where everything is coloured says nothing.
    """
    body = _fence_safe("\n".join(_clamp(x) for x in lines if x is not None))
    return "```diff\n" + body + "\n```"


_STATE_MARK = {"done": "+", "error": "-", "interrupted": "-"}


def _headline(f: dict, state: str, frame: int | None = None) -> str:
    """The status row. It lives INSIDE the card now (the owner 2026-08-11) — a bold
    line floating above a code block reads as two separate messages, and on a
    narrow phone it wrapped away from the block it belongs to.

    Inside a fence there is no bold, so the outcome is carried by the diff
    marker instead: green when it finished, red when it did not, plain while it
    is still going.
    """
    glyph = (_SPINNER[frame % len(_SPINNER)]
             if state == "running" and frame is not None
             else GLYPHS.get(state, "•"))
    head = f"{_STATE_MARK.get(state, ' ')} {glyph} {_HEADS.get(state, state)}"
    # WHO is driving, in the headline rather than buried in the fact line: with
    # four bots on the same cockpit (claude-a, claude-b, gpt, gemma) the first
    # question about any run is whose it is (the owner 2026-08-06).
    if f.get("bot"):
        head += f" · {f['bot']}"
    head += f" · {_fmt_duration(f.get('duration_s'))}"
    # A clean exit's reason is "exit 0", which tells nobody anything. Every
    # other terminal reason is the most useful word in the message.
    if state not in ("done", "running") and f.get("reason"):
        head += f" · {_clip(str(f['reason']), 120)}"
    return head


def _fact_line(f: dict) -> str:
    surface = f.get("surface") or ""
    if surface:
        surface = (f"{_SURFACE_EMOJI.get(surface, '🧭')} "
                   f"{_SURFACE_LABEL.get(surface, surface)}")
    facts = [x for x in (surface, f.get("model"), f.get("effort")) if x]
    if f.get("n_messages"):
        facts.append(f"{f['n_messages']} msgs")
    if f.get("tokens"):
        facts.append(f"{_fmt_tokens(f['tokens'])} tok")
    return " · ".join(facts)


def format_ping(f: dict) -> str:
    """The finished-run message — one card, nothing outside it.

    2026-08-05 put the heading in markdown above the block; 2026-08-11 pulled it
    in, so the whole ping is a single monospaced readout instead of a bold line
    with a box under it.
    """
    state = f.get("state") or "done"
    out = _block([
        _headline(f, state),
        "",
        " " + _clip(str(f.get("task") or ""), TASK_LIMIT),
        " " + _fact_line(f),
    ])
    return out[:DISCORD_LIMIT]


def live_body(f: dict, steps: list[tuple[str, str]]) -> str:
    """The card's content WITHOUT the headline — used as the change signal.

    The headline carries elapsed time and the spinner frame, both of which move
    on their own; comparing the whole message would make every poll look like a
    change. Change is what the agent DID, not what the clock did.
    """
    return "\n".join(_live_rows(f, list(steps or [])))


def _current_row(steps: list, f: dict) -> str:
    """What Operator is doing RIGHT NOW — one line, not the whole history.

    Until 2026-08-11 this printed every action as it completed, so a chatty run
    grew a wall of Clicking/Took screenshot rows that pushed the card past the
    fence width and buried the only thing you actually want off a glance: what
    it is doing this second. The cockpit's own minimised status has always been
    one line ("Reading", "Working") and the alert now reads the same way.

    The trace is not lost — it is in the cockpit, which is where you go when you
    want the history rather than the state.
    """
    if not steps:
        return " (starting)"
    label, detail, ts = steps[-1]
    end = float(f.get("ended_ts") or 0) or time.time()
    dur = _fmt_duration(max(0.0, end - float(ts or 0))) if ts else ""
    if str(label) == "⚠":
        # An error IS the row's glyph — printing "●  ⚠  Action failed" marks the
        # same row twice and reads as two events. Red, because it is one.
        return _clamp(f"- ⚠  {_clip(str(detail), 60)}")
    glyph = _STEP_LIVE if (f.get("state") or "") == "running" else _STEP_DONE
    row = f" {glyph}  {label}"
    if detail:
        row += f"  {_clip(str(detail), 52)}"
    return _clamp(row + (f"  {dur}" if dur else ""))


def _live_rows(f: dict, steps: list) -> list[str]:
    """Everything under the headline. Split out so live_body can compare the
    parts that represent work without the clock and spinner moving underneath
    it every poll."""
    runtime_note = ("(agy has no live trace — final text only)"
                    if f.get("runtime") == "agy" and not steps else None)
    return [
        "",
        " " + _clip(str(f.get("task") or ""), TASK_LIMIT),
        "",
        " " + runtime_note if runtime_note else _current_row(steps, f),
        "",
        " " + _fact_line(f),
    ]


def format_live(f: dict, steps: list | None = None, frame: int | None = None) -> str:
    """The in-flight card, edited in place while the run works.

    One current-activity line rather than a growing trace, so the card is a
    fixed size no matter how chatty the run is. That also retires the old
    budget-and-drop machinery: there is nothing left to overflow, and no
    "+N earlier" to declare, because the card never claimed to be the history.
    """
    body = [_headline(f, f.get("state") or "running", frame)]
    body += _live_rows(f, list(steps or []))
    return _block(body)[:DISCORD_LIMIT]


def live_steps(runner) -> list[tuple[str, str, float]]:
    """(label, detail, ts) for every action the run has taken, plus any error
    line — the same rows the cockpit's trace renders, with the timestamp so the
    card can show how long each step took."""
    try:
        msgs = list(getattr(runner, "messages", None) or [])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for m in msgs:
        ts = float(m.get("ts") or 0)
        if m.get("role") == "action" and m.get("text"):
            out.append((str(m["text"]), str(m.get("detail") or ""), ts))
        elif m.get("role") == "error" and m.get("text"):
            out.append(("⚠", str(m["text"]), ts))
    return out


def should_ping(f: dict) -> bool:
    """Demo runs never ping. Demo/prod isolation is a safety property (1.0.16)
    and a public visitor's run must not reach a private phone."""
    return not f.get("demo")


def facts(runner, reason: str = "") -> dict:
    """Runner object → the flat dict everything above reads. Same attribute
    names the flight recorder pulls, so the ping and the ledger row can never
    describe the same run differently."""
    started = float(getattr(runner, "started_ts", 0) or 0)
    ended = float(getattr(runner, "ended_ts", 0) or 0) or time.time()
    return {
        "state": str(getattr(runner, "state", "") or ""),
        "reason": reason or "",
        "task": str(getattr(runner, "task", "") or ""),
        "bot": str(getattr(runner, "bot", "") or ""),
        "model": str(getattr(runner, "model", "") or ""),
        "effort": str(getattr(runner, "effort", "") or ""),
        "surface": str(getattr(runner, "surface", "") or ""),
        "runtime": str(getattr(runner, "_runtime", "") or ""),
        "demo": bool(getattr(runner, "demo", False)),
        "n_messages": len(getattr(runner, "messages", None) or []),
        "tokens": getattr(runner, "_cum_in_tokens", None),
        "started_ts": started,
        "ended_ts": ended,
        "duration_s": max(0.0, ended - started),
    }


# ── the final frame ─────────────────────────────────────────────────────

def latest_shot(since: float, until: float, dirs=None) -> str | None:
    """Newest image written while the run was alive, or None.

    Scoped by mtime rather than by parsing the trace: every runtime writes
    its screenshots to these dirs under different names, and the single-flight
    runner (a documented safety property) means only one run can own the
    window.
    """
    if dirs is None:
        try:
            from operator_trace import shot_dirs
            dirs = shot_dirs()
        except Exception:  # noqa: BLE001
            return None
    best, best_ts = None, -1.0
    for d in dirs or []:
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        with entries:
            for e in entries:
                if not e.is_file() or not e.name.lower().endswith(_IMAGE_EXT):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                if st.st_size > _MAX_UPLOAD or not (since <= st.st_mtime <= until):
                    continue
                if st.st_mtime > best_ts:
                    best, best_ts = e.path, st.st_mtime
    return best


# ── transport ───────────────────────────────────────────────────────────

def _token() -> str | None:
    if tok := os.environ.get(HELPER_ENV, "").strip():
        return tok
    # Resolved from the same file host-app reads, so the cockpit needs no
    # secret of its own. Deliberately not an import of host-app's card
    # module: that pulls the whole store in behind it.
    try:
        with open(os.path.expanduser(_HELPER_FILE), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(HELPER_ENV + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def _multipart(payload: dict, image_path: str) -> tuple[bytes, str]:
    boundary = "----operator" + uuid.uuid4().hex
    name = os.path.basename(image_path)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    with open(image_path, "rb") as fh:
        blob = fh.read()
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
        f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\";"
        f" filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode(),
        blob, b"\r\n", f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _send(url: str, method: str, body: bytes, ctype: str) -> tuple[bool, str, str | None]:
    """(ok, error, message_id). One shot, no retry — the run already happened
    and a missed ping is cheap."""
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Authorization": f"Bot {_token()}", "Content-Type": ctype,
                 "User-Agent": "operator-ping (1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            ok = 200 <= resp.status < 300
            mid = None
            try:
                mid = json.loads(resp.read()).get("id")
            except Exception:  # noqa: BLE001
                pass
            return (ok, "", mid)
    except urllib.error.HTTPError as e:
        return (False, f"HTTP {e.code}: {e.read()[:160]!r}", None)
    except Exception as e:  # noqa: BLE001
        return (False, f"{type(e).__name__}: {e}", None)


def _post(channel: str, text: str, image_path: str | None) -> tuple[bool, str, str | None]:
    payload = {"content": text, "allowed_mentions": {"parse": []}}
    if image_path:
        body, ctype = _multipart(payload, image_path)
    else:
        body, ctype = json.dumps(payload).encode(), "application/json"
    return _send(f"https://discord.com/api/v10/channels/{channel}/messages",
                 "POST", body, ctype)


def _edit(channel: str, message_id: str, text: str) -> tuple[bool, str, str | None]:
    body = json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode()
    return _send(
        f"https://discord.com/api/v10/channels/{channel}/messages/{message_id}",
        "PATCH", body, "application/json")


# ── entry points ────────────────────────────────────────────────────────

def _target() -> str | None:
    """The channel to post into, or None when pings are off / unconfigured."""
    channel = os.environ.get(CHANNEL_ENV, "").strip()
    return channel if channel and _token() else None


def notify(runner, reason: str = "") -> bool:
    """Post one ping for a finished run. True only if Discord accepted it.
    Never raises: a notification is not worth a run."""
    try:
        channel = _target()
        if not channel:
            return False
        f = facts(runner, reason)
        if not should_ping(f):
            return False
        shot = latest_shot(f["started_ts"], f["ended_ts"] + 5)
        ok, err, _mid = _post(channel, format_ping(f), shot)
        if not ok:
            log.warning("operator ping failed (run unaffected): %s", err)
        return ok
    except Exception as e:  # noqa: BLE001 — by contract: never break a run
        log.warning("operator ping failed (run unaffected): %s", e)
        return False


def watch(runner, now=time.monotonic, sleep=time.sleep) -> str | None:
    """Post an in-flight card and keep editing it while the run works.

    Returns the message id it drove, or None if it never posted. The clock and
    sleep are injectable so the loop is testable without real time passing.

    Edits, not new messages, on purpose: an edit doesn't buzz a phone, so the
    channel stays quiet until the run actually finishes and notify() posts the
    completion ping as a NEW message. One card per run, one buzz per run.
    """
    channel = _target()
    if not channel:
        return None
    f = facts(runner)
    if not should_ping(f):
        return None
    frame = 0
    ok, err, mid = _post(channel, format_live(f, live_steps(runner), frame), None)
    if not ok or not mid:
        log.warning("operator live card failed (run unaffected): %s", err)
        return None

    started = last_edit = now()
    last_body = live_body(f, live_steps(runner))
    while True:
        sleep(LIVE_POLL_S)
        f, steps = facts(runner), live_steps(runner)
        running = str(getattr(runner, "state", "")) == "running"
        if not running or (now() - started) > LIVE_MAX_S:
            # Always retire the card, even for a run that did nothing: its
            # headline still says Running and that would stay on screen. No
            # frame — a finished run gets its ✓/✕/⏹ back, not a stopped wheel.
            _edit(channel, mid, format_live(f, steps))
            return mid
        body = live_body(f, steps)
        moved = body != last_body and (now() - last_edit) >= LIVE_EDIT_S
        # Spin even when nothing happened: a frozen wheel reads as a dead run,
        # which is exactly what the wheel is there to rule out.
        spin = (now() - last_edit) >= LIVE_SPIN_S
        if moved or spin:
            frame += 1
            _edit(channel, mid, format_live(f, steps, frame))
            last_body, last_edit = body, now()


def watch_async(runner) -> None:
    """Start the in-flight card off the run thread. Same contract as the ping:
    a watcher failure must never touch the run."""
    try:
        threading.Thread(target=_watch_guarded, args=(runner,),
                         name="operator-live", daemon=True).start()
    except Exception as e:  # noqa: BLE001
        log.warning("operator live thread failed (run unaffected): %s", e)


def _watch_guarded(runner) -> None:
    try:
        watch(runner)
    except Exception as e:  # noqa: BLE001
        log.warning("operator live card failed (run unaffected): %s", e)


def notify_async(runner, reason: str = "") -> None:
    """Fire the ping off the run thread. _set_state is the sole state writer
    and runs on the run thread; a slow Discord must not hold a terminal
    transition open behind an HTTP timeout."""
    try:
        threading.Thread(target=notify, args=(runner, reason),
                         name="operator-ping", daemon=True).start()
    except Exception as e:  # noqa: BLE001
        log.warning("operator ping thread failed (run unaffected): %s", e)
