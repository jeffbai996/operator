"""operator_session — the cockpit's conversations, persisted server-side.

The chat log / mode / picker state used to live only in each browser's
localStorage, so every device had its own unrelated history. v1.0.11 moved it
to one shared server-side session: whoever opens the cockpit, on whatever
device, sees the same conversation.

One was not enough (the owner 2026-08-06). Every task landed in the same
transcript, an unrelated errand's context rode along inside it, and the only
way to get a clean start was the trash can, which destroyed what was there.
So this module now owns a MAP of conversations plus which one is active, and
the cockpit gets a switcher. `load()` and `save()` default to the active one
for legacy callers and accept an explicit conversation id for independent
browsers and runners.

On-disk shape (v3). `rev` is the store/list revision; each conversation has
its own `rev` for optimistic writes from multiple devices:

    {"rev": 12, "active": "<id>",
     "sessions": {"<id>": {"rev": 4, "title": str,
                              "updated_ts": float, "data": {...}}}}

A v1 file ({"rev", "data"}) migrates into one conversation on first read.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import tempfile
import time
import uuid

log = logging.getLogger("operator.session")

# the session payload mirrors the client's localStorage shape (log HTML, mode,
# bot/model/effort) — the log dominates. The client trims its log to ~80
# nodes; 1MB is far above any legitimate payload and far below abuse size.
# Applies PER CONVERSATION: one runaway chat can't be saved, the others are
# unaffected.
MAX_BYTES = 1_000_000
TITLE_LIMIT = 80
PREVIEW_LIMIT = 180
META_LIMIT = 40

# .demo backstop: same-user demo server must never read/write the owner's
# session (routes are 403 in demo, but the suffix removes the shared file too).
_PATH = os.environ.get(
    "OPERATOR_SESSION_PATH",
    os.path.join(os.path.expanduser("~/.cache/computer-use"),
                 "operator-session.json")
    + (".demo" if os.environ.get("OPERATOR_DEMO") else ""))
_LOCK = threading.Lock()
_PRESENCE_LOCK = threading.Lock()
_PRESENCE: dict[str, dict] = {}
PRESENCE_TTL = max(5.0, float(os.environ.get("OPERATOR_PRESENCE_TTL", "15")))
_clock = time.monotonic


_LEGACY_ID = "legacy"


class SessionConflict(Exception):
    """An optimistic write was based on an older conversation revision."""

    def __init__(self, current: dict) -> None:
        super().__init__("conversation changed on another device")
        self.current = current


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _mtime() -> float:
    """The session file's own mtime — a migrated conversation should carry the
    time it was last written, not the time it happened to be read."""
    try:
        return os.path.getmtime(_PATH)
    except OSError:
        return time.time()


def _blank() -> dict:
    return {"schema": 3, "rev": 0, "active": "", "sessions": {}}


def _read_unlocked() -> dict:
    """The whole store in v2 shape. Absent, corrupt or v1 files all resolve to
    something usable — a bad session file must never be a dead cockpit."""
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return _blank()
    except Exception as e:  # noqa: BLE001 — corrupt file ≠ dead cockpit
        log.warning("session file unreadable (%s) — starting fresh", e)
        return _blank()
    if not isinstance(raw, dict):
        return _blank()
    rev = raw.get("rev") if isinstance(raw.get("rev"), int) else 0
    sessions = raw.get("sessions")
    if isinstance(sessions, dict):
        clean = {}
        for key, value in sessions.items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            item["rev"] = max(0, int(item.get("rev") or 0))
            clean[key] = item
        active = raw.get("active") if raw.get("active") in clean else ""
        if not active and clean:
            active = _newest(clean)
        return {"schema": 3, "rev": rev, "active": active, "sessions": clean}
    # v1 → v2: the one shared session becomes the first conversation, keeping
    # its rev so a client mid-flight doesn't see the counter go backwards.
    # Its id is FIXED, not generated: migration happens on every read until
    # something writes, and a fresh random id per read means a client can list
    # a conversation and then 404 activating it a second later (caught live,
    # 2026-08-06).
    data = raw.get("data")
    if isinstance(data, dict):
        return {"schema": 3, "rev": rev, "active": _LEGACY_ID,
                "sessions": {_LEGACY_ID: {"rev": 1, "title": "",
                                          "updated_ts": _mtime(), "data": data}}}
    return {"rev": rev, "active": "", "sessions": {}}


def _newest(sessions: dict) -> str:
    return max(sessions, key=lambda k: sessions[k].get("updated_ts") or 0)


def _write_unlocked(state: dict) -> int:
    """Atomic (tmp+rename) so a crash mid-write can't corrupt what was there."""
    state["rev"] = int(state.get("rev") or 0) + 1
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_PATH),
                               prefix=".session-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, _PATH)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return state["rev"]


def _clip_title(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= TITLE_LIMIT else text[: TITLE_LIMIT - 1] + "…"


def _clip_meta(value: object, limit: int) -> str:
    """Plain-text list metadata; transcripts never belong in the list API."""
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── the single-session contract the client already speaks ───────────────

def load(conversation_id: str | None = None) -> dict:
    """Store revision, per-conversation revision and data for one thread."""
    with _LOCK:
        st = _read_unlocked()
        sid = conversation_id or st["active"]
        if (conversation_id and sid not in st["sessions"]
                and sid != _LEGACY_ID):
            raise KeyError(sid)
        sess = st["sessions"].get(sid) or {}
        return {"rev": st["rev"], "conversation_rev": int(sess.get("rev") or 0),
                "data": sess.get("data")}


def save(data: dict, conversation_id: str | None = None,
         expected_rev: int | None = None) -> int:
    """Persist into the active conversation; returns the new revision. Creates
    the first conversation if there is none. Raises ValueError over MAX_BYTES."""
    if not isinstance(data, dict):
        raise ValueError("session data must be an object")
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"session payload exceeds {MAX_BYTES} bytes")
    with _LOCK:
        st = _read_unlocked()
        sid = conversation_id or st["active"] or _new_id()
        # A completely empty store is advertised by the route as the stable
        # `legacy` conversation so the client has an identity before its first
        # debounced save. Let that one seed the store; arbitrary unknown ids
        # still fail instead of silently manufacturing a typo'd conversation.
        if (conversation_id and sid not in st["sessions"]
                and sid != _LEGACY_ID):
            raise KeyError(sid)
        prev = st["sessions"].get(sid) or {"title": "", "rev": 0}
        current_rev = int(prev.get("rev") or 0)
        if expected_rev is not None:
            try:
                expected = int(expected_rev)
            except (TypeError, ValueError) as exc:
                raise ValueError("expected_rev must be an integer") from exc
            if expected != current_rev:
                raise SessionConflict({
                    "rev": st["rev"], "conversation_rev": current_rev,
                    "data": prev.get("data")})
        st["sessions"][sid] = {"rev": current_rev + 1,
                               "title": prev.get("title") or "",
                               "updated_ts": time.time(), "data": data}
        if not st["active"]:
            st["active"] = sid
        return _write_unlocked(st)


def active_id() -> str:
    with _LOCK:
        return _read_unlocked()["active"]


# ── conversations ───────────────────────────────────────────────────────

def _is_empty(sess: dict) -> bool:
    data = sess.get("data")
    return not isinstance(data, dict) or not (data.get("log") or "").strip()


def listing() -> dict:
    """Compact, safe conversation metadata, newest first.

    ``data.log`` is intentionally excluded: the library needs a preview, not a
    second copy of every transcript (or a raw-HTML injection surface).
    """
    with _LOCK:
        st = _read_unlocked()
        rows = []
        for sid, sess in st["sessions"].items():
            data = sess.get("data") if isinstance(sess.get("data"), dict) else {}
            rows.append({
                "id": sid,
                "title": sess.get("title") or "",
                "conversation_rev": int(sess.get("rev") or 0),
                "updated_ts": sess.get("updated_ts") or 0,
                "empty": _is_empty(sess),
                "preview": _clip_meta(data.get("preview"), PREVIEW_LIMIT),
                "bot": _clip_meta(data.get("bot"), META_LIMIT),
                "surface": _clip_meta(data.get("surface"), META_LIMIT),
            })
        rows.sort(key=lambda r: r["updated_ts"], reverse=True)
        return {"rev": st["rev"], "active": st["active"], "sessions": rows}


def create(title: str = "") -> dict:
    """Get or create the one reusable empty draft and make it active.

    Repeated taps and concurrent devices must not manufacture a trail of blank
    chats. Once the draft has transcript content, the next call creates a new
    identity.
    """
    with _LOCK:
        st = _read_unlocked()
        empty = [(sid, sess) for sid, sess in st["sessions"].items()
                 if _is_empty(sess)]
        if empty:
            sid, _sess = max(
                empty, key=lambda item: item[1].get("updated_ts") or 0)
            st["active"] = sid
            return {"id": sid, "rev": _write_unlocked(st), "reused": True}
        sid = _new_id()
        st["sessions"][sid] = {"rev": 0, "title": _clip_title(title),
                               "updated_ts": time.time(), "data": None}
        st["active"] = sid
        return {"id": sid, "rev": _write_unlocked(st), "reused": False}


def activate(sid: str) -> dict:
    """Switch conversations. Returns {rev, data} so the caller can paint the
    chat in the same round trip. KeyError if the id is unknown."""
    with _LOCK:
        st = _read_unlocked()
        if sid not in st["sessions"]:
            raise KeyError(sid)
        st["active"] = sid
        rev = _write_unlocked(st)
        return {"rev": rev,
                "conversation_rev": int(st["sessions"][sid].get("rev") or 0),
                "data": st["sessions"][sid].get("data")}


def rename(sid: str, title: str) -> int:
    with _LOCK:
        st = _read_unlocked()
        if sid not in st["sessions"]:
            raise KeyError(sid)
        st["sessions"][sid]["title"] = _clip_title(title)
        return _write_unlocked(st)


def delete(sid: str) -> dict:
    """Drop a conversation. Deleting the active one falls through to the most
    recent survivor; deleting the last one leaves a fresh empty conversation,
    so the cockpit always has somewhere to write. {active, rev}."""
    with _LOCK:
        st = _read_unlocked()
        if sid not in st["sessions"]:
            raise KeyError(sid)
        st["sessions"].pop(sid)
        if st["active"] == sid or st["active"] not in st["sessions"]:
            if st["sessions"]:
                st["active"] = _newest(st["sessions"])
            else:
                new = _new_id()
                st["sessions"][new] = {"rev": 0, "title": "",
                                       "updated_ts": time.time(),
                                       "data": None}
                st["active"] = new
        return {"active": st["active"], "rev": _write_unlocked(st)}


def title_if_unset(text: str, conversation_id: str | None = None) -> None:
    """Name the active conversation after the first task dispatched into it.

    Titling server-side rather than in the client because the server is where
    the task text is known for certain — the client's copy is HTML by then.
    Best-effort: a titling failure must never break a dispatch.
    """
    try:
        title = _clip_title(text)
        if not title:
            return
        with _LOCK:
            st = _read_unlocked()
            sid = conversation_id or st["active"]
            sess = st["sessions"].get(sid)
            if not sess or (sess.get("title") or "").strip():
                return
            sess["title"] = title
            _write_unlocked(st)
    except Exception as e:  # noqa: BLE001
        log.warning("session auto-title failed (run unaffected): %s", e)


# ── cross-device control lease ──────────────────────────────────────────

def _presence_result(current: dict, client_id: str, now: float) -> dict:
    return {
        "can_control": current.get("client_id") == client_id,
        "controller_label": current.get("label") or "another device",
        "lease_expires_in": max(0.0, round(PRESENCE_TTL - (now - current["seen"]), 1)),
    }


def touch_presence(conversation_id: str, client_id: str, label: str = "",
                   *, take_over: bool = False) -> dict:
    """Claim/renew a thread's editing lease or observe its current owner.

    The lease is deliberately process-local: it describes live browser tabs,
    expires quickly after a device disappears, and must not survive a deploy.
    """
    sid = str(conversation_id or "").strip() or _LEGACY_ID
    client = str(client_id or "").strip()[:80]
    if not client:
        raise ValueError("client_id is required")
    clean_label = " ".join(str(label or "").split())[:40] or "another device"
    with _LOCK:
        state = _read_unlocked()
        if sid not in state["sessions"] and sid != _LEGACY_ID:
            raise KeyError(sid)
    now = _clock()
    with _PRESENCE_LOCK:
        current = _PRESENCE.get(sid)
        expired = current is None or now - current["seen"] >= PRESENCE_TTL
        if expired or take_over or current.get("client_id") == client:
            current = {"client_id": client, "label": clean_label, "seen": now}
            _PRESENCE[sid] = current
        return _presence_result(current, client, now)


def presence(conversation_id: str, client_id: str = "") -> dict:
    sid = str(conversation_id or "").strip() or _LEGACY_ID
    now = _clock()
    with _PRESENCE_LOCK:
        current = _PRESENCE.get(sid)
        if current is None or now - current["seen"] >= PRESENCE_TTL:
            _PRESENCE.pop(sid, None)
            return {"can_control": False, "controller_label": "",
                    "lease_expires_in": 0.0}
        return _presence_result(current, str(client_id or "").strip(), now)
