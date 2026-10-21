"""Background browser leases and retained frames, independent of the cockpit."""
from __future__ import annotations

from functools import lru_cache
import importlib.util
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import urllib.parse

LOG = logging.getLogger(__name__)
BACKGROUND_GRACE_SEC = 60
_pending_idle_shutdown = False


@lru_cache(maxsize=1)
def tabs():
    path = Path(__file__).resolve().parents[1] / 'browse/operator_browser_tabs.py'
    spec = importlib.util.spec_from_file_location('operator_background_tab_registry', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def background_targets() -> set[str]:
    import operator_session
    hidden = operator_session.background_ids()
    return {target for cid, ids in tabs()._read_registry(tabs().DEFAULT_REGISTRY).items()
            if cid in hidden for target in ids}


def prepare(cid: str, endpoint: str, ensure_browser) -> str | None:
    """Prove Chrome's renderer without starting a cockpit capture loop."""
    try:
        ensure_browser()
        client = tabs().TabCleanupClient(endpoint, timeout=3)
        row = next(r for r in client._get('/json/list') if r.get('type') == 'page')
        client._rpc(row['webSocketDebuggerUrl'], 'Runtime.evaluate', {
            'expression': 'document.readyState', 'returnByValue': True})
        return None
    except Exception:
        LOG.warning('Background browser preparation failed')
        return 'background browser could not connect'


def open_start_page(cid: str, endpoint: str, url: str, ensure_browser) -> str | None:
    """Open a saved task's start page on the conversation's own page.

    RunnerRegistry calls this after the run's slot is reserved and while its
    admission lock is held, so a refused run never navigates any page and a
    running conversation's page is never touched. The CDP calls are bounded
    because that lock also serves the cockpit. Returns an error, or None."""
    if '://' not in url:
        url = 'https://' + url
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        return "the task's start page must be an http or https address"
    try:
        ensure_browser()
        helper = tabs()
        target = helper.reserve(cid, endpoint, helper.DEFAULT_REGISTRY)
        client = helper.TabCleanupClient(endpoint, timeout=5)
        row = next(r for r in client._get('/json/list') if r.get('id') == target)
        client._rpc(row['webSocketDebuggerUrl'], 'Page.navigate', {'url': url})
        return None
    except Exception:
        LOG.warning('A saved task start page could not be opened')
        return "the task's start page could not be opened"


def _capture_path(cid: str) -> Path:
    # IDs are server-generated, but never allow a raw API argument into a path.
    if not cid or len(cid) > 100 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in cid):
        raise ValueError('invalid conversation id')
    return tabs().DEFAULT_REGISTRY.parent / 'operator-browser-captures' / (cid + '.json')


def clear_capture(cid: str) -> None:
    try:
        _capture_path(cid).unlink(missing_ok=True)
    except (OSError, ValueError):
        LOG.warning('Could not clear a prior browser capture')


def _save_capture(cid: str, shot: dict) -> None:
    path = _capture_path(cid)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.capture-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(shot, handle)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def look(cid: str, endpoint: str) -> dict | None:
    """One owned frame. A closed browser returns its last frame, never relaunches."""
    helper = tabs()
    targets = helper._read_registry(helper.DEFAULT_REGISTRY).get(cid, [])
    if targets:
        try:
            client = helper.TabCleanupClient(endpoint, timeout=3)
            rows = {r.get('id'): r for r in client._get('/json/list')}
            for target in targets:
                row = rows.get(target)
                if not row or row.get('type') != 'page':
                    continue
                image = client._rpc(row['webSocketDebuggerUrl'], 'Page.captureScreenshot', {
                    'format': 'jpeg', 'quality': 65, 'fromSurface': True,
                    'captureBeyondViewport': False})
                if not image.get('data'):
                    continue
                shot = {'jpeg_b64': image['data'], 'url': row.get('url') or '',
                        'title': row.get('title') or '', 'captured_ts': time.time(), 'cached': False}
                import operator_session
                window = client._rpc(client._get('/json/version')['webSocketDebuggerUrl'],
                                     'Browser.getWindowForTarget', {'targetId': target})
                if (operator_session.is_background(cid)
                        and window['bounds'].get('windowState') == 'minimized'):
                    _save_capture(cid, shot)
                return shot
        except Exception:
            LOG.debug('Background browser capture unavailable')
    try:
        shot = json.loads(_capture_path(cid).read_text())
        return {**shot, 'cached': True} if shot.get('jpeg_b64') else None
    except (OSError, ValueError, AttributeError):
        return None


def can_release(cid: str) -> bool:
    import operator_session
    import operator_workspace
    if (not operator_session.is_background(cid)
            or operator_session.presence(cid).get('lease_expires_in', 0)):
        return False
    state = operator_workspace.snapshot(cid)
    if any(transfer.get('status') == 'downloading' for transfer in state.get('transfers', [])):
        return False
    jobs = state['jobs']
    # Approval and handoff are pauses, not completion. Preserve the browser
    # that produced the pending action until the workflow is resolved.
    return not any(job.get('state') == 'needs_input'
                   or any(a.get('status') == 'pending' for a in job.get('approvals', []))
                   for job in jobs)


def reopen(cid: str, endpoint: str, ensure_browser) -> None:
    """An explicit Inspect can reopen the last URL after its live tabs expire."""
    ensure_browser()
    helper = tabs()
    client = helper.TabCleanupClient(endpoint, timeout=3)
    rows = {r.get('id'): r for r in client._get('/json/list')}
    owned = helper._read_registry(helper.DEFAULT_REGISTRY).get(cid, [])
    if any(target in rows for target in owned):
        return
    try:
        url = json.loads(_capture_path(cid).read_text()).get('url', '')
    except (OSError, ValueError, AttributeError):
        url = ''
    target = helper.reserve(cid, endpoint, helper.DEFAULT_REGISTRY, background=True)
    if url.startswith(('http://', 'https://')):
        row = next(r for r in client._get('/json/list') if r.get('id') == target)
        client._rpc(row['webSocketDebuggerUrl'], 'Page.navigate', {'url': url})


def park(cid: str, endpoint: str) -> bool:
    """Return an explicitly inspected job to its private minimized windows."""
    helper = tabs()
    targets = set(helper._read_registry(helper.DEFAULT_REGISTRY).get(cid, []))
    if not targets:
        return True
    client = helper.TabCleanupClient(endpoint, timeout=2)
    ws = client._get('/json/version')['webSocketDebuggerUrl']
    pages = client._get('/json/list')
    windows = {}
    for row in pages:
        if row.get('type') == 'page':
            window = client._rpc(ws, 'Browser.getWindowForTarget', {'targetId': row['id']})
            windows.setdefault(window['windowId'], set()).add(row['id'])
    for window, ids in windows.items():
        if ids & targets:
            # Historical jobs may share a human window. Never minimize it.
            if not ids.issubset(targets):
                return False
            client._rpc(ws, 'Browser.setWindowBounds', {
                'windowId': window, 'bounds': {'windowState': 'minimized'}})
    return True


def release(cid: str, endpoint: str) -> int:
    """Close only exclusively owned minimized pages; failed closes retain ownership."""
    helper = tabs()
    path = helper.DEFAULT_REGISTRY
    lock = helper._locked(path)
    try:
        registry = helper._read_registry(path)
        targets = list(registry.get(cid, []))
        if not targets:
            return 0
        client = helper.TabCleanupClient(endpoint, timeout=2)
        pages = {r['id']: r for r in client._get('/json/list') if r.get('type') == 'page'}
        ws = client._get('/json/version')['webSocketDebuggerUrl']
        closed = 0
        for target in targets[:4]:
            if target not in pages:
                registry[cid].remove(target)
                continue
            if any(target in ids for owner, ids in registry.items() if owner != cid):
                continue
            window = client._rpc(ws, 'Browser.getWindowForTarget', {'targetId': target})
            if window['bounds'].get('windowState') != 'minimized':
                continue  # native-window interaction is also a human handoff
            if len(pages) <= 1:
                helper._new_target(endpoint)  # preserve the shared browser for other callers
            client.close(target)
            deadline = time.monotonic() + 1
            while target in {r.get('id') for r in client._get('/json/list')}:
                if time.monotonic() >= deadline:
                    break
                time.sleep(.05)
            else:
                registry[cid].remove(target)
                pages.pop(target, None)
                closed += 1
        registry = {owner: ids for owner, ids in registry.items() if ids}
        helper._write_registry(registry, path)
        return closed
    finally:
        lock.close()


def sweep(view) -> dict:
    """The server owns cleanup even if an MCP caller disconnects or times out."""
    global _pending_idle_shutdown
    runner = view.operator_agent.runner
    closed = 0
    owned = tabs()._read_registry(tabs().DEFAULT_REGISTRY)
    for cid, generation in runner.background_cleanup_candidates().items():
        if cid not in owned or time.time() - generation[1] < BACKGROUND_GRACE_SEC:
            continue
        def finish():
            nonlocal closed
            if not can_release(cid):
                return False
            # Keep a final screenshot before releasing the live resource.
            look(cid, view.CDP_URL)
            closed += release(cid, view.CDP_URL)
            return True
        try:
            runner.with_background_cleanup_lease(cid, generation, finish)
        except Exception:
            LOG.warning('Background browser release failed; ownership retained')
    if closed:
        _pending_idle_shutdown = True
    stopped = False
    if _pending_idle_shutdown:
        try:
            stopped = runner.with_idle_browser_lease(lambda: _close_idle_browser(view))
            if stopped:
                _pending_idle_shutdown = False
        except Exception:
            LOG.debug('Background browser shutdown deferred')
    return {'closed': closed, 'browser_stopped': stopped}


def _close_idle_browser(view) -> bool:
    import operator_session
    import operator_workspace
    now = time.monotonic()
    if any(now - feed.last_view < 90 for feed in (view._streamer, view._desktop_feed)):
        return False
    if tabs()._read_registry(tabs().DEFAULT_REGISTRY):
        return False
    if any(operator_session.presence(row['id']).get('lease_expires_in', 0)
           for row in operator_session.listing()['sessions']):
        return False
    with operator_workspace.database() as db:
        if db.execute("SELECT 1 FROM transfers WHERE status='downloading' LIMIT 1").fetchone():
            return False
    client = tabs().TabCleanupClient(view.CDP_URL, timeout=2)
    pages = client.pages()
    if any(row.get('attached', True) or not view._is_blank_url(row.get('url'))
           for row in pages.values()):
        return False
    for row in pages.values():
        focused = client._rpc(row['webSocketDebuggerUrl'], 'Runtime.evaluate', {
            'expression': 'document.hasFocus()', 'returnByValue': True})
        if focused.get('exceptionDetails') or focused.get('result', {}).get('value') is not False:
            return False
    ws = client._get('/json/version')['webSocketDebuggerUrl']
    try:
        client._rpc(ws, 'Browser.close')
    except Exception:
        # Some Chrome builds close the socket before sending the reply. Prove
        # the endpoint is gone before calling that accepted shutdown a success.
        try:
            client._get('/json/version')
        except OSError:
            return True
        raise
    return True
