"""events.py — append operator-cockpit trace events from the control layer.

Same NDJSON contract as browse/mcp_action_tap.py (the playwright-MCP tee): one
{bot, tool, action, detail, ts} object per line into the shared event log the
operator page tails. The tap covers browser_* tools; THIS covers the control
MCP's own tools (perceive / game_macro / computer) plus the controller's
per-op progress, so macro execution shows live in the cockpit trace.

Best-effort by contract: a logging failure must never break the tool call.
"""
from __future__ import annotations

import json
import os
import time

EVENT_LOG = os.path.expanduser("~/.cache/computer-use/operator-events.ndjson")
_MAX_LINES = 500
_MIN_INTERVAL_S = 0.15   # throttle: macro ops can fire many per second
_last_write = 0.0


def record(bot: str, tool: str, action: str, detail: str = "",
           throttle: bool = False) -> None:
    """Append one event. With throttle=True, drops events arriving faster than
    _MIN_INTERVAL_S (per-op macro progress must not flood the log)."""
    global _last_write
    try:
        now = time.time()
        if throttle and (now - _last_write) < _MIN_INTERVAL_S:
            return
        _last_write = now
        evt = {"bot": bot or "unknown", "tool": tool, "action": action,
               "detail": (detail or "")[:120], "ts": now}
        os.makedirs(os.path.dirname(EVENT_LOG), exist_ok=True)
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
        _truncate()
    except Exception:
        pass


def _truncate() -> None:
    try:
        with open(EVENT_LOG, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > _MAX_LINES:
            with open(EVENT_LOG, "w", encoding="utf-8") as f:
                f.writelines(lines[-_MAX_LINES:])
    except Exception:
        pass
