#!/usr/bin/env python3
"""Own Chrome page targets per Operator conversation.

The browser profile is intentionally shared (cookies, 1Password, adblock), but
the pages a runtime is allowed to see are not.  A tiny locked registry gives
each conversation a stable root target plus every popup or explicit new tab it
opens.  Closed/stale targets are replaced; another conversation's targets are
never adopted.  Deleting a conversation can then close the whole owned set.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.parse
import urllib.request


DEFAULT_REGISTRY = Path(os.environ.get(
    "OPERATOR_BROWSER_TABS_PATH",
    os.path.expanduser("~/.cache/computer-use/operator-browser-tabs.json")))


def _targets(value: object) -> list[str]:
    """Normalise v1's ``conversation -> target`` registry to a target list."""
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    out: list[str] = []
    for target in raw:
        target = str(target or "").strip()
        if target and target not in out:
            out.append(target)
    return out


def _read_registry(path: Path = DEFAULT_REGISTRY) -> dict[str, list[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): targets for k, value in raw.items() if k
            if (targets := _targets(value))}


def _write_registry(data: dict[str, list[str]], path: Path = DEFAULT_REGISTRY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            clean = {str(cid): _targets(targets)
                     for cid, targets in data.items() if _targets(targets)}
            json.dump(clean, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _json(endpoint: str, suffix: str, *, method: str = "GET"):
    url = endpoint.rstrip("/") + suffix
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def _command(endpoint: str, suffix: str) -> None:
    """Run a status-only Chrome debug command.

    Unlike ``/json/list`` and ``/json/new``, Chrome's activate/close endpoints
    answer with plain text. Treat a successful HTTP status as the contract;
    attempting ``json.load`` here made live tab activation fail after a
    perfectly successful reservation.
    """
    url = endpoint.rstrip("/") + suffix
    with urllib.request.urlopen(url, timeout=5):
        pass


def _page_targets(endpoint: str) -> set[str]:
    rows = _json(endpoint, "/json/list")
    return {str(row.get("id") or row.get("targetId")) for row in rows
            if row.get("type") == "page" and (row.get("id") or row.get("targetId"))}


def _new_target(endpoint: str) -> str:
    # Chrome's /json/new endpoint requires PUT.  about:blank keeps a new
    # conversation neutral and avoids leaking the conversation id in history.
    row = _json(endpoint, "/json/new?" + urllib.parse.quote("about:blank", safe=":"),
                method="PUT")
    target = str(row.get("id") or row.get("targetId") or "")
    if not target:
        raise RuntimeError("Chrome created a tab without a target id")
    return target


def _close_target(endpoint: str, target: str) -> None:
    try:
        _command(endpoint, "/json/close/" + urllib.parse.quote(target, safe=""))
    except Exception:
        # Releasing ownership must still succeed if the user already closed it.
        pass


def _activate_target(endpoint: str, target: str) -> None:
    _command(endpoint, "/json/activate/" + urllib.parse.quote(target, safe=""))


def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(str(path) + ".lock", "a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def reserve(conversation_id: str, endpoint: str,
            path: Path = DEFAULT_REGISTRY) -> str:
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        raise ValueError("conversation id is required")
    lock = _locked(path)
    try:
        registry = _read_registry(path)
        live = _page_targets(endpoint)
        targets = registry.get(conversation_id, [])
        target = next((target for target in targets if target in live), "")
        if target:
            # Write the v1 -> v2 shape while this conversation is already
            # touching the lock, so a later popup claim has one canonical form.
            registry[conversation_id] = targets
            _write_registry(registry, path)
            return target
        target = _new_target(endpoint)
        registry[conversation_id] = [target]
        _write_registry(registry, path)
        return target
    finally:
        lock.close()


def claim(conversation_id: str, target: str, path: Path = DEFAULT_REGISTRY) -> bool:
    """Record a popup or ``newPage`` target as belonging to a conversation.

    The wrapper calls this the instant Playwright exposes a descendant page.
    It intentionally does not ask Chrome whether the target is live: a popup
    can appear between that probe and the write, and a stale entry is harmless
    because ``release`` treats close as best-effort.
    """
    conversation_id = str(conversation_id or "").strip()
    target = str(target or "").strip()
    if not conversation_id or not target:
        raise ValueError("conversation id and target are required")
    lock = _locked(path)
    try:
        registry = _read_registry(path)
        targets = registry.setdefault(conversation_id, [])
        if target not in targets:
            targets.append(target)
            _write_registry(registry, path)
        return True
    finally:
        lock.close()


def release(conversation_id: str, endpoint: str,
            path: Path = DEFAULT_REGISTRY, *, close: bool = False) -> bool:
    lock = _locked(path)
    try:
        registry = _read_registry(path)
        targets = registry.pop(str(conversation_id), [])
        if not targets:
            return False
        _write_registry(registry, path)
        if close:
            # Closing Chrome's final page can end the interactive browser and
            # take the shared signed-in profile down with it.  Park one neutral
            # page first only when this conversation owns every live page.
            try:
                live = _page_targets(endpoint)
            except Exception:
                live = set()
            if live and live.issubset(set(targets)):
                try:
                    _new_target(endpoint)
                except Exception:
                    # Do not turn a best-effort cleanup into a browser outage.
                    # Chrome normally creates this target; if it refuses, leave
                    # the final owned page intact rather than closing Chrome.
                    targets = targets[:-1]
            for target in targets:
                _close_target(endpoint, target)
        return True
    finally:
        lock.close()


def activate(conversation_id: str, endpoint: str,
             path: Path = DEFAULT_REGISTRY) -> bool:
    lock = _locked(path)
    try:
        targets = _read_registry(path).get(str(conversation_id), [])
        target = targets[0] if targets else ""
        if not target or target not in _page_targets(endpoint):
            return False
        _activate_target(endpoint, target)
        return True
    finally:
        lock.close()


# Fail closed on visible pages, edited forms, rich-text editors and playback.
# This is a read-only probe; it installs no listeners or scripts in the page.
IDLE_PAGE_EXPRESSION = """(() => {
  if (document.visibilityState !== 'hidden' || document.hasFocus()) return false;
  // Cross-origin frames can contain edits this document cannot inspect.
  if (document.querySelector('iframe,frame')) return false;
  if (document.querySelector('[contenteditable]:not([contenteditable="false"])')) return false;
  for (const e of document.querySelectorAll('input,textarea,select')) {
    if (e.tagName === 'SELECT') {
      if ([...e.options].some(o => o.selected !== o.defaultSelected)) return false;
    } else if (e.value !== e.defaultValue || e.checked !== e.defaultChecked) return false;
  }
  return ![...document.querySelectorAll('video,audio')].some(e => !e.paused);
})()"""


class TabCleanupClient:
    """Small bounded CDP probes; never starts Chrome or a Playwright driver."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip('/')

    def _get(self, suffix: str):
        with urllib.request.urlopen(self.endpoint + suffix, timeout=1) as response:
            return json.load(response)

    @staticmethod
    def _rpc(url: str, method: str, params: dict | None = None) -> dict:
        import websocket
        ws = websocket.create_connection(url, timeout=1, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                ws.settimeout(max(0.01, deadline - time.monotonic()))
                reply = json.loads(ws.recv())
                if reply.get("id") == 1:
                    if "error" in reply:
                        raise RuntimeError("CDP cleanup probe rejected")
                    return reply["result"]
            raise TimeoutError("CDP cleanup probe timed out")
        finally:
            ws.close(timeout=0)

    def pages(self) -> dict[str, dict]:
        rows = self._get('/json/list')
        version = self._get('/json/version')
        infos = self._rpc(version['webSocketDebuggerUrl'], 'Target.getTargets')['targetInfos']
        attached = {row['targetId']: row.get('attached', True) for row in infos}
        return {row['id']: {**row, 'attached': attached.get(row['id'], True)}
                for row in rows if row.get('type') == 'page' and row.get('id')}

    def idle_page(self, row: dict) -> bool:
        result = self._rpc(row['webSocketDebuggerUrl'], 'Runtime.evaluate', {
            'expression': IDLE_PAGE_EXPRESSION, 'returnByValue': True,
            'timeout': 500, 'throwOnSideEffect': True})
        return not result.get('exceptionDetails') and result.get('result', {}).get('value') is True

    def close(self, target: str) -> None:
        with urllib.request.urlopen(self.endpoint + '/json/close/' +
                                    urllib.parse.quote(target, safe=''), timeout=1):
            pass


class IdleTabReaper:
    """Reap only proven finished, exclusively owned, hidden, untouched pages.

    Idle proof is deliberately volatile: a server restart must grant a fresh
    grace period, not trust a persisted timestamp while it wasn't observing.
    Unknown historical runners are NOT inferred to be finished. One close and
    four page probes per sweep bound work on the shared host.
    """

    def __init__(self, path: Path = DEFAULT_REGISTRY, idle_seconds: float = 1200) -> None:
        self.path = path
        self.idle_seconds = idle_seconds
        self._idle: dict[str, tuple] = {}
        self._cursor = 0

    def reset(self) -> None:
        self._idle.clear()

    def sweep(self, client, candidates: dict, guard, *, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        stats = dict(owned=0, eligible=0, closed=0, errors=0)
        # No registry means no ownership. Don't even wake/probe the browser.
        if not self.path.exists():
            self.reset()
            return stats
        handle = open(str(self.path) + '.lock', 'a+', encoding='utf-8')
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.reset()
                return stats
            registry = _read_registry(self.path)
            stats['owned'] = sum(map(len, registry.values()))
            if not registry or not candidates:
                self.reset()
                return stats
            pages = client.pages()
            original = {cid: list(ids) for cid, ids in registry.items()}
            owners: dict[str, list[str]] = {}
            for cid, ids in registry.items():
                for tid in ids:
                    owners.setdefault(tid, []).append(cid)
            eligible = {tid: cids[0] for tid, cids in owners.items()
                        if len(cids) == 1 and cids[0] in candidates and tid in pages}
            stats['eligible'] = len(eligible)
            self._idle = {tid: value for tid, value in self._idle.items()
                          if tid in eligible}
            ids = sorted(eligible)
            if ids:
                offset = self._cursor % len(ids)
                batch = (ids[offset:] + ids[:offset])[:4]
                self._cursor = (offset + len(batch)) % len(ids)
            else:
                batch = []
            for tid in batch:
                cid, row = eligible[tid], pages[tid]
                fingerprint = (cid, candidates[cid], row.get('url'))
                if row.get('attached', True) or not client.idle_page(row):
                    self._idle.pop(tid, None)
                    continue
                previous = self._idle.get(tid)
                if previous is None or previous[0] != fingerprint:
                    self._idle[tid] = (fingerprint, now)
                    continue
                if now - previous[1] < self.idle_seconds:
                    continue

                def close_if_still_idle() -> bool:
                    # The caller holds run admission here. Recheck native tab
                    # visibility immediately before closing, not at scan start.
                    current = client.pages()
                    fresh = current.get(tid)
                    if (len(current) <= 1 or not fresh or fresh.get('attached', True)
                            or fresh.get('url') != row.get('url')
                            or not client.idle_page(fresh)):
                        self._idle.pop(tid, None)
                        return False
                    client.close(tid)  # failure must retain registry ownership
                    # An accepted close can still be held by beforeunload.
                    # Do not turn that page into an unowned orphan.
                    return tid not in client.pages()

                if guard(cid, candidates[cid], close_if_still_idle):
                    pages.pop(tid, None)
                    self._idle.pop(tid, None)
                    stats['closed'] += 1
                    break
                self._idle.pop(tid, None)
            registry = {cid: [tid for tid in ids if tid in pages]
                        for cid, ids in registry.items()}
            if registry != original:
                _write_registry(registry, self.path)
        except Exception:
            # Missing browser, invalid response or close failure is not
            # evidence of abandonment. Preserve ownership and retry later.
            self.reset()
            stats['errors'] += 1
        finally:
            handle.close()
        return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("reserve", "claim", "release", "activate"))
    parser.add_argument("conversation_id")
    parser.add_argument("endpoint")
    parser.add_argument("target_id", nargs="?")
    parser.add_argument("--close", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "reserve":
        print(reserve(args.conversation_id, args.endpoint))
    elif args.action == "claim":
        if not args.target_id:
            parser.error("claim needs a target id")
        print("1" if claim(args.conversation_id, args.target_id) else "0")
    elif args.action == "release":
        release(args.conversation_id, args.endpoint, close=args.close)
    else:
        print("1" if activate(args.conversation_id, args.endpoint) else "0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
