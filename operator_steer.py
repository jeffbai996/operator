"""operator_steer.py — the mid-run steer queue (1.0.12).

A steer is a user message sent while a run is live. Two consumers, two seams:
  * steer_hook.py (PostToolUse, claude runtime) injects queued steers as
    additionalContext right after the agent's next tool call — mid-loop,
  * the runner's exit-seam check turns leftovers into one more resumed turn
    (the only seam codex/agy have — they expose no mid-loop input channel).

The hook runs inside the spawned agent process. A conversation-scoped flock
serializes append, claim and clear across processes: an append cannot land in
an already-claimed file after its consumer has read it. Claim still uses an
atomic rename, so only one consumer receives a correction.
"""
from __future__ import annotations

import json
import contextlib
import fcntl
import os
import re
import time

MAX_TEXT = 4000      # one steer's text cap (it rides inside a prompt)
MAX_PENDING = 8      # queue cap — more than this means nobody's listening


@contextlib.contextmanager
def _queue_lock(filename):
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename + '.lock', 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

_DEFAULT = os.path.join(os.path.expanduser("~/.cache/computer-use"),
                        "operator-steer.ndjson")


def _scope(conversation_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", conversation_id).strip("-.")
    return clean[:80] or "legacy"


def path(conversation_id: str | None = None) -> str:
    p = os.environ.get("OPERATOR_STEER_PATH")
    if not p:
        # DEMO ISOLATION (review finding 2026-07-11): the demo server runs as the
        # same user — an unscoped default would share this queue with the real
        # cockpit, letting a demo visitor's steer reach a live production run.
        # The launch scripts set OPERATOR_STEER_PATH; this is the backstop.
        p = _DEFAULT + (".demo" if os.environ.get("OPERATOR_DEMO") else "")
    cid = conversation_id or os.environ.get("OPERATOR_CONVERSATION_ID") or ""
    if cid and cid != "legacy":
        return p + ".conversations/" + _scope(cid) + ".ndjson"
    return p


def _read(p: str) -> list[dict]:
    out: list[dict] = []
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(d, dict) and isinstance(d.get("text"), str):
                    item = {"ts": d.get("ts", 0.0), "text": d["text"]}
                    if d.get('id'):
                        item['id'] = d['id']
                    out.append(item)
    except OSError:
        pass
    return out


def push(text: str, conversation_id: str | None = None, message_id: str = '') -> int:
    """Queue one steer; returns the pending count. Raises ValueError on
    empty/oversize text or a full queue (the caller surfaces it to the UI)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty steer")
    if len(text) > MAX_TEXT:
        raise ValueError(f"steer too long (max {MAX_TEXT} chars)")
    p = path(conversation_id)
    with _queue_lock(p):
        n = len(_read(p))
        if n >= MAX_PENDING:
            raise ValueError(f"steer queue full ({MAX_PENDING} pending)")
        line = json.dumps({"ts": time.time(), "text": text, 'id': message_id}) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    return n + 1


def pending(conversation_id: str | None = None) -> list[dict]:
    return _read(path(conversation_id))


def take_all(conversation_id: str | None = None) -> list[dict]:
    """Atomically claim and return every queued steer ([] when none)."""
    p = path(conversation_id)
    claim = f"{p}.claim.{os.getpid()}.{time.monotonic_ns()}"
    with _queue_lock(p):
        try:
            os.rename(p, claim)
        except OSError:
            return []
        out = _read(claim)
        try:
            os.unlink(claim)
        except OSError:
            pass
    return out


def clear(conversation_id: str | None = None) -> None:
    p = path(conversation_id)
    with _queue_lock(p):
        try:
            os.unlink(p)
        except OSError:
            pass


def format_context(steers: list[dict]) -> str:
    """The framing both seams wrap around steer text before it reaches the
    model — unmistakably the human, unmistakably mid-run."""
    body = "\n".join("- " + s["text"] for s in steers)
    return ("MID-RUN MESSAGE FROM THE USER (sent through the operator cockpit "
            "while you were working — incorporate it into what you're doing "
            "right now; it may change or refine the task):\n" + body)


def followup_prompt(steers: list[dict]) -> str:
    """The exit-seam variant: the run just finished its turn, so frame the
    steers as the next instruction in the same conversation."""
    body = "\n".join("- " + s["text"] for s in steers)
    return ("[The user sent this while you were working — it arrived as you "
            "were finishing. Continue the same conversation and act on it:]\n"
            + body)
