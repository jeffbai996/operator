"""operator_session — the ONE shared cockpit session, persisted server-side.

The chat log / mode / picker state used to live only in each browser's
localStorage, so every device had its own unrelated history. The live cockpit
is a single-user, single-session product (2026-07-11): whoever opens it,
on whatever device, should see the same conversation. This module owns that
state: a small JSON file with a monotonic revision counter the client uses to
decide whether the server copy supersedes its local cache.

Deliberately NOT multi-session — one file, last write wins. Concurrent-viewer
live sync is out of scope (1.0.15 multi-viewer territory); this covers
"walk from the desk to the couch and the chat is still there".
"""
from __future__ import annotations

import json
import logging
import os
import threading
import tempfile

log = logging.getLogger("operator.session")

# the session payload mirrors the client's localStorage shape (log HTML, mode,
# bot/model/effort) — the log dominates. The client trims its log to ~80
# nodes; 1MB is far above any legitimate payload and far below abuse size.
MAX_BYTES = 1_000_000

# .demo backstop: same-user demo server must never read/write the owner's
# session (routes are 403 in demo, but the suffix removes the shared file too).
_PATH = os.environ.get(
    "OPERATOR_SESSION_PATH",
    os.path.join(os.path.expanduser("~/.cache/computer-use"),
                 "operator-session.json")
    + (".demo" if os.environ.get("OPERATOR_DEMO") else ""))
_LOCK = threading.Lock()


def load() -> dict:
    """Return {rev, data}; {rev: 0, data: None} when absent or unreadable."""
    with _LOCK:
        try:
            with open(_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and isinstance(raw.get("rev"), int):
                return {"rev": raw["rev"], "data": raw.get("data")}
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 — corrupt file ≠ dead cockpit
            log.warning("session file unreadable (%s) — starting fresh", e)
        return {"rev": 0, "data": None}


def save(data: dict) -> int:
    """Persist the session payload; returns the new revision. Atomic
    (tmp+rename) so a crash mid-write can't corrupt the previous session.
    Raises ValueError when the payload exceeds MAX_BYTES."""
    if not isinstance(data, dict):
        raise ValueError("session data must be an object")
    blob_probe = json.dumps(data, ensure_ascii=False)
    if len(blob_probe.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"session payload exceeds {MAX_BYTES} bytes")
    with _LOCK:
        rev = 0
        try:
            with open(_PATH, "r", encoding="utf-8") as f:
                prev = json.load(f)
            if isinstance(prev, dict) and isinstance(prev.get("rev"), int):
                rev = prev["rev"]
        except Exception:  # noqa: BLE001 — absent/corrupt → rev restarts
            pass
        rev += 1
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_PATH),
                                   prefix=".session-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"rev": rev, "data": data}, f, ensure_ascii=False)
            os.replace(tmp, _PATH)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        return rev
