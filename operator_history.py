"""operator_history — the flight recorder (one SQLite row per finished run).

Until 1.0.11 a run's outcome evaporated at the next dispatch: the transcript
file holds only the LAST run, and nothing kept task/model/surface/tokens/
terminal-reason together. This ledger records every run at its terminal
transition (running → done/error/interrupted, hooked in operator_agent's
_set_state) so the cockpit can answer "what ran, what did it cost, why did
it stop" long after the fact.

Contract: record() is BEST-EFFORT and never raises — a history failure must
never break a live run. Reads are lean by default (recent() carries no trace
payload; get() returns the full row).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger("operator.history")

# traces are the bulky column; cap keeps rows cheap while preserving the TAIL
# of the run (the terminal turns are the part worth reading later)
MAX_TRACE_BYTES = 400_000

# .demo backstop: the demo server runs as the same user — demo visitors' runs
# must never write rows into the owner's ledger (launch scripts set the env).
_PATH = os.environ.get(
    "OPERATOR_HISTORY_PATH",
    os.path.join(os.path.expanduser("~/.cache/computer-use"),
                 "operator-history.db")
    + (".demo" if os.environ.get("OPERATOR_DEMO") else ""))
_LOCK = threading.Lock()

_SCHEMA = """CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts REAL, ended_ts REAL,
    bot TEXT, task TEXT, state TEXT, reason TEXT,
    model TEXT, effort TEXT, runtime TEXT, surface TEXT,
    demo INTEGER DEFAULT 0,
    cum_in_tokens INTEGER, peak_in_tokens INTEGER,
    n_messages INTEGER, trace TEXT)"""

_LEAN_COLS = ("id", "started_ts", "ended_ts", "bot", "task", "state",
              "reason", "model", "effort", "runtime", "surface", "demo",
              "cum_in_tokens", "peak_in_tokens", "n_messages")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    c = sqlite3.connect(_PATH, timeout=5)
    c.execute(_SCHEMA)
    return c


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _capped_trace(messages) -> str:
    """JSON trace capped to MAX_TRACE_BYTES by dropping the OLDEST entries —
    default=str so an exotic object in a message can't kill the record."""
    msgs = list(messages or [])[-400:]
    while msgs:
        blob = json.dumps(msgs, ensure_ascii=False, default=str)
        if len(blob.encode("utf-8")) <= MAX_TRACE_BYTES:
            return blob
        msgs = msgs[max(1, len(msgs) // 5):]
    return "[]"


def record(runner, reason: str = "") -> int | None:
    """Persist one finished run off the live runner object. Never raises."""
    try:
        started = _to_float(getattr(runner, "started_ts", None))
        ended = _to_float(getattr(runner, "ended_ts", None)) or time.time()
        msgs = getattr(runner, "messages", None) or []
        row = (
            started, ended,
            str(getattr(runner, "bot", "") or ""),
            str(getattr(runner, "task", "") or ""),
            str(getattr(runner, "state", "") or ""),
            str(reason or ""),
            str(getattr(runner, "model", "") or ""),
            str(getattr(runner, "effort", "") or ""),
            str(getattr(runner, "_runtime", "") or ""),
            str(getattr(runner, "surface", "") or ""),
            1 if getattr(runner, "demo", False) else 0,
            _to_int(getattr(runner, "_cum_in_tokens", None)),
            _to_int(getattr(runner, "_peak_in_tokens", None)),
            len(msgs),
            _capped_trace(msgs),
        )
        with _LOCK:
            with _conn() as c:
                cur = c.execute(
                    "INSERT INTO runs (started_ts, ended_ts, bot, task, state,"
                    " reason, model, effort, runtime, surface, demo,"
                    " cum_in_tokens, peak_in_tokens, n_messages, trace)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
                return cur.lastrowid
    except Exception as e:  # noqa: BLE001 — by contract: never break a run
        log.warning("history record failed (run unaffected): %s", e)
        return None


def recent(limit: int = 50) -> list[dict]:
    """Newest-first lean rows (no trace payload) + computed duration_s."""
    try:
        with _LOCK:
            with _conn() as c:
                rows = c.execute(
                    f"SELECT {', '.join(_LEAN_COLS)} FROM runs"
                    " ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(zip(_LEAN_COLS, r))
            d["duration_s"] = (round(d["ended_ts"] - d["started_ts"], 1)
                               if d.get("started_ts") and d.get("ended_ts")
                               else None)
            out.append(d)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("history read failed: %s", e)
        return []


def get(run_id: int) -> dict | None:
    """Full row incl. parsed trace, or None."""
    try:
        with _LOCK:
            with _conn() as c:
                r = c.execute(
                    f"SELECT {', '.join(_LEAN_COLS)}, trace FROM runs"
                    " WHERE id = ?", (int(run_id),)).fetchone()
        if r is None:
            return None
        d = dict(zip(_LEAN_COLS, r[:-1]))
        d["duration_s"] = (round(d["ended_ts"] - d["started_ts"], 1)
                           if d.get("started_ts") and d.get("ended_ts")
                           else None)
        try:
            d["trace"] = json.loads(r[-1] or "[]")
        except Exception:  # noqa: BLE001
            d["trace"] = []
        return d
    except Exception as e:  # noqa: BLE001
        log.warning("history read failed: %s", e)
        return None
