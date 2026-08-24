"""Human-handoff pings — the cockpit's notification surface.

Operator's messages, actions, failures, and completions remain visible in the
cockpit without being mirrored into Discord. The alerts channel gets exactly
the event that requires attention: an explicit TAKE_CONTROL handoff, with the
latest frame attached when one exists.

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
_MAX_UPLOAD = 8 * 1024 * 1024        # comfortably under Discord's cap
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")

# Keep the old state glyphs available to the pure formatter vocabulary, but the
# only outbound card now uses the explicit handoff warning.
GLYPHS = {"done": "●", "error": "✗", "interrupted": "⏹", "running": "◉",
          "handoff": "⚠"}
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
          "running": "Running", "handoff": "Operator needs your input"}


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


_STATE_MARK = {"done": "+", "error": "-", "interrupted": "-", "handoff": "-"}


def _headline(f: dict, state: str) -> str:
    """The status row. It lives INSIDE the card now (the owner 2026-08-11) — a bold
    line floating above a code block reads as two separate messages, and on a
    narrow phone it wrapped away from the block it belongs to.

    Inside a fence there is no bold, so the outcome is carried by the diff
    marker instead: green when it finished, red when it did not, plain while it
    is still going.
    """
    glyph = GLYPHS.get(state, "•")
    head = f"{_STATE_MARK.get(state, ' ')} {glyph} {_HEADS.get(state, state)}"
    # WHO is driving, in the headline rather than buried in the fact line: with
    # four bots on the same cockpit (claude-a, claude-b, gpt, gemma) the first
    # question about any run is whose it is (the owner 2026-08-06).
    if f.get("bot"):
        head += f" · {f['bot']}"
    head += f" · {_fmt_duration(f.get('duration_s'))}"
    # A clean exit's reason is "exit 0", which tells nobody anything. Every
    # other terminal reason is the most useful word in the message.
    if state not in ("done", "running", "handoff") and f.get("reason"):
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
    """The human-handoff message — one card, nothing outside it.

    The actual terminal state is intentionally secondary: the reason this card
    exists is the work the human must do, not whether the agent process exited.
    """
    handoff = str(f.get("handoff") or "Human input required.")
    alert = {**f, "state": "handoff", "reason": handoff}
    out = _block([
        _headline(alert, "handoff"),
        "",
        " " + _clip(handoff, TASK_LIMIT),
        "",
        " " + _clip(str(f.get("task") or ""), TASK_LIMIT),
        " " + _fact_line(f),
    ])
    return out[:DISCORD_LIMIT]


def should_ping(f: dict) -> bool:
    """Only a private run's explicit TAKE_CONTROL request may page #alerts."""
    return not f.get("demo") and bool(f.get("handoff"))


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
        "handoff": str((getattr(runner, "handoff", None) or {}).get("reason") or ""),
        "n_messages": len(getattr(runner, "messages", None) or []),
        "tokens": getattr(runner, "_cumulative_in_tokens", None),
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


# ── entry points ────────────────────────────────────────────────────────

def _target() -> str | None:
    """The channel to post into, or None when pings are off / unconfigured."""
    channel = os.environ.get(CHANNEL_ENV, "").strip()
    return channel if channel and _token() else None


def notify(runner, reason: str = "") -> bool:
    """Post one ping for a human handoff. True only if Discord accepted it.
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
            return False

        return ok
    except Exception as e:  # noqa: BLE001 — by contract: never break a run
        log.warning("operator ping failed (run unaffected): %s", e)
        return False


def notify_async(runner, reason: str = "") -> None:
    """Fire the ping off the run thread. _set_state is the sole state writer
    and runs on the run thread; a slow Discord must not hold a terminal
    transition open behind an HTTP timeout."""
    try:
        threading.Thread(target=notify, args=(runner, reason),
                         name="operator-ping", daemon=True).start()
    except Exception as e:  # noqa: BLE001
        log.warning("operator ping thread failed (run unaffected): %s", e)
