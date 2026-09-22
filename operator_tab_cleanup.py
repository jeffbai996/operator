"""Owned-tab housekeeping, started by the server, never by importing routes."""
from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
import threading
import time

LOG = logging.getLogger(__name__)
_thread: threading.Thread | None = None
_start_lock = threading.Lock()
_status: dict = {"enabled": False, "closed_total": 0, "idle_seconds": 1200}


def status() -> dict:
    return dict(_status)


def start(view) -> None:
    global _thread
    if view.DEMO or os.environ.get("OPERATOR_TESTING"):
        return
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        here = Path(__file__).resolve()
        helper = next((p for p in (
            here.parents[1] / "browse" / "operator_browser_tabs.py",
            here.parent / "browse" / "operator_browser_tabs.py") if p.is_file()), None)
        if helper is None:
            return  # standalone demo export has no private browser plumbing
        spec = importlib.util.spec_from_file_location("operator_owned_tab_helper", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reaper = module.IdleTabReaper()
        client = module.TabCleanupClient(view.CDP_URL)
        _status["enabled"] = True
        _thread = threading.Thread(target=_loop, args=(view, reaper, client),
                                   name="operator-tab-cleanup", daemon=True)
        _thread.start()


def sweep(view, reaper, client) -> dict:
    import operator_session
    runner = view.operator_agent.runner

    def viewing() -> bool:
        now = time.monotonic()
        return any(now - feed.last_view < 90 for feed in (view._streamer, view._desktop_feed))

    if viewing():
        reaper.reset()
        return {"paused": "viewer", "closed": 0}
    candidates = {cid: generation for cid, generation in runner.tab_cleanup_candidates().items()
                  if not operator_session.presence(cid).get("lease_expires_in", 0)}

    def guard(cid, generation, action) -> bool:
        def checked() -> bool:
            if viewing() or operator_session.presence(cid).get("lease_expires_in", 0):
                return False
            return action()
        return runner.with_tab_cleanup_lease(cid, generation, checked)

    return reaper.sweep(client, candidates, guard)


def _loop(view, reaper, client) -> None:
    # No browser launches, per-tick subprocesses or filesystem scans. A stopped
    # browser is simply unavailable; failures contain no URLs or credentials.
    while True:
        time.sleep(60)
        try:
            result = sweep(view, reaper, client)
            _status.update(last_sweep=time.time(), last=result,
                           closed_total=_status["closed_total"] + result.get("closed", 0))
            if result.get("closed"):
                LOG.info("Operator owned-tab cleanup: closed %d finished tab", result["closed"])
        except Exception:
            reaper.reset()
            _status.update(last_sweep=time.time(), last={"errors": 1})
            LOG.warning("Operator owned-tab cleanup probe failed; tabs preserved")
