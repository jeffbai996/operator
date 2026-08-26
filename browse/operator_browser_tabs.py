#!/usr/bin/env python3
"""Reserve one Chrome page target per Operator conversation.

The browser profile is intentionally shared (cookies, 1Password, adblock), but
the page a runtime is allowed to see is not.  A tiny locked registry gives each
conversation a stable CDP target across follow-up turns and concurrent MCP
processes.  Closed/stale targets are replaced; another conversation's target
is never adopted.
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


def _read_registry(path: Path = DEFAULT_REGISTRY) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k and v}


def _write_registry(data: dict[str, str], path: Path = DEFAULT_REGISTRY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"), sort_keys=True)
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
        _json(endpoint, "/json/close/" + urllib.parse.quote(target, safe=""))
    except Exception:
        # Releasing ownership must still succeed if the user already closed it.
        pass


def _activate_target(endpoint: str, target: str) -> None:
    _json(endpoint, "/json/activate/" + urllib.parse.quote(target, safe=""))


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
        target = registry.get(conversation_id, "")
        if target in live:
            return target
        target = _new_target(endpoint)
        registry[conversation_id] = target
        _write_registry(registry, path)
        return target
    finally:
        lock.close()


def release(conversation_id: str, endpoint: str,
            path: Path = DEFAULT_REGISTRY, *, close: bool = False) -> bool:
    lock = _locked(path)
    try:
        registry = _read_registry(path)
        target = registry.pop(str(conversation_id), "")
        if not target:
            return False
        _write_registry(registry, path)
        if close:
            _close_target(endpoint, target)
        return True
    finally:
        lock.close()


def activate(conversation_id: str, endpoint: str,
             path: Path = DEFAULT_REGISTRY) -> bool:
    lock = _locked(path)
    try:
        target = _read_registry(path).get(str(conversation_id), "")
        if not target or target not in _page_targets(endpoint):
            return False
        _activate_target(endpoint, target)
        return True
    finally:
        lock.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("reserve", "release", "activate"))
    parser.add_argument("conversation_id")
    parser.add_argument("endpoint")
    parser.add_argument("--close", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "reserve":
        print(reserve(args.conversation_id, args.endpoint))
    elif args.action == "release":
        release(args.conversation_id, args.endpoint, close=args.close)
    else:
        print("1" if activate(args.conversation_id, args.endpoint) else "0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
