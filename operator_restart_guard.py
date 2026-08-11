"""Defer host-app restarts while an Operator turn owns the process.

The guard marker is intentionally file-backed: the PreToolUse hook runs in the
agent process, while the terminal transition runs inside host-app-server.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


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
