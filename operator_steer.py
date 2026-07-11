"""operator_steer.py — the mid-run steer queue (1.0.12).

A steer is a user message sent while a run is live. Two consumers, two seams:
  * steer_hook.py (PostToolUse, claude runtime) injects queued steers as
    additionalContext right after the agent's next tool call — mid-loop,
  * the runner's exit-seam check turns leftovers into one more resumed turn
    (the only seam codex/agy have — they expose no mid-loop input channel).

The hook runs inside the SPAWNED AGENT PROCESS, not the server, so this store
must be safe across processes without a shared lock:
  * push()    — one NDJSON line via O_APPEND (atomic for small writes),
  * take_all()— claim by os.rename to a unique temp name (atomic: exactly one
                of two racing consumers gets the file), then read + unlink.
A push racing a claim normally lands in a fresh file and is picked up by the
next consumer; double-delivery is impossible (rename is the atomic claim).
Loss is possible only in a tight window (append fd obtained before a rename,
write landing after the claimed file is read) — irrelevant at human steer
rates, so we accept it rather than add locking.
"""
from __future__ import annotations

import json
import os
import time

MAX_TEXT = 4000      # one steer's text cap (it rides inside a prompt)
MAX_PENDING = 8      # queue cap — more than this means nobody's listening

_DEFAULT = os.path.join(os.path.expanduser("~/.cache/computer-use"),
                        "operator-steer.ndjson")


def path() -> str:
    p = os.environ.get("OPERATOR_STEER_PATH")
    if p:
        return p
    # DEMO ISOLATION (review finding 2026-07-11): the demo server runs as the
    # same user — an unscoped default would share this queue with the real
    # cockpit, letting a demo visitor's steer reach a live production run.
    # The launch scripts set OPERATOR_STEER_PATH; this is the backstop.
    if os.environ.get("OPERATOR_DEMO"):
        return _DEFAULT + ".demo"
    return _DEFAULT


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
                    out.append({"ts": d.get("ts", 0.0), "text": d["text"]})
    except OSError:
        pass
    return out


def push(text: str) -> int:
    """Queue one steer; returns the pending count. Raises ValueError on
    empty/oversize text or a full queue (the caller surfaces it to the UI)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty steer")
    if len(text) > MAX_TEXT:
        raise ValueError(f"steer too long (max {MAX_TEXT} chars)")
    p = path()
    n = len(_read(p))
    if n >= MAX_PENDING:
        raise ValueError(f"steer queue full ({MAX_PENDING} pending)")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    line = json.dumps({"ts": time.time(), "text": text}) + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)
    return n + 1


def pending() -> list[dict]:
    return _read(path())


def take_all() -> list[dict]:
    """Atomically claim and return every queued steer ([] when none)."""
    p = path()
    claim = f"{p}.claim.{os.getpid()}.{time.monotonic_ns()}"
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


def clear() -> None:
    try:
        os.unlink(path())
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
