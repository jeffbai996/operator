"""Defer host-app restarts while an Operator turn owns the process.

The guard marker is intentionally file-backed: the PreToolUse hook runs in the
agent process, while the terminal transition runs inside host-app-server.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import fcntl


UNIT = "host-app-server.service"
_RESTART_RE = re.compile(
    r"(?:^|[;&|]\s*)systemctl\s+--user\s+(?:restart|try-restart)\s+"
    r"(?:[^;&|]*\s)?host-app-server(?:\.service)?(?:\s|$|[;&|])"
)


def marker_path() -> Path:
    return Path(os.environ.get(
        "OPERATOR_RESTART_MARKER",
        os.path.expanduser("~/.cache/computer-use/operator-restart-pending.json"),
    ))


def drain_path() -> Path:
    return Path(os.environ.get(
        "OPERATOR_DRAIN_PATH",
        os.path.expanduser("~/.cache/computer-use/operator-deploy-drain"),
    ))


@contextmanager
def admission_lock(*, exclusive: bool = False):
    path = Path(str(drain_path()) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock_file.fileno(), mode)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def wait_until_idle(url: str, *, opener=urlopen, sleep=time.sleep) -> bool:
    """Block a service stop until every in-process Operator run is terminal.

    The drain marker intentionally survives this helper: it covers the small
    gap between ExecStop returning and systemd actually terminating the Flask
    process. ExecStartPre removes it for the replacement process.
    """
    path = drain_path()
    with admission_lock(exclusive=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps({"pid": os.getpid(), "started": time.time()}),
                       encoding="utf-8")
        os.replace(tmp, path)
        failures = 0
        announced = ""
        while True:
            try:
                with opener(url, timeout=3) as response:
                    payload = json.loads(response.read())
                failures = 0
            except Exception as exc:  # the service may already be unhealthy
                failures += 1
                if failures >= 3:
                    print(f"Operator drain probe failed; allowing recovery: {exc}",
                          file=sys.stderr)
                    return True
                sleep(0.5)
                continue

            admission = payload.get("admission", {})
            active = int(admission.get("active", 0) or 0)
            if active <= 0:
                return True
            jobs = ", ".join(
                f"{job.get('conversation_id', '?')} ({job.get('bot', '?')})"
                for job in admission.get("jobs", [])) or str(active)
            if jobs != announced:
                print(f"Waiting for active Operator run(s): {jobs}",
                      file=sys.stderr)
                announced = jobs
            sleep(0.5)


def requests_server_restart(command: str) -> bool:
    """Return true only for a direct user-systemd restart of this service."""
    return bool(_RESTART_RE.search(command or ""))


def defer(command: str = "") -> None:
    """Atomically record one coalesced restart request."""
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"requested_ts": time.time(), "command": command}),
                   encoding="utf-8")
    os.replace(tmp, path)


def consume_and_restart(run: Any = subprocess.run) -> bool:
    """Consume a queued request and ask systemd to restart after this turn.

    The request is removed before spawning so repeated terminal transitions
    cannot bounce the cockpit twice. A failed spawn restores the marker.
    """
    path = marker_path()
    try:
        payload = path.read_text(encoding="utf-8")
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False

    def _restart() -> None:
        # Give the terminal response poll a brief chance to observe `done`.
        time.sleep(0.35)
        try:
            run(["systemctl", "--user", "restart", UNIT], check=True,
                capture_output=True, text=True, timeout=15)
        except Exception:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload, encoding="utf-8")
            except OSError:
                pass

    threading.Thread(target=_restart, daemon=True,
                     name="operator-deferred-restart").start()
    return True


def _main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--wait-idle":
        return 0 if wait_until_idle(argv[1]) else 1
    print("usage: operator_restart_guard.py --wait-idle <agent-state-url>",
          file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
