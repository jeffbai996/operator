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
