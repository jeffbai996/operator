"""Owned Chrome downloads and Windows/WSL file staging.

The shared Chrome profile gets ONE managed download destination. Ownership is
resolved from the initiating frame, never the foreground tab or a URL. Only
completed GUID files with an ownership record enter the durable chat store.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

import operator_workspace as workspace

_BROWSE = str(Path(__file__).resolve().parent / 'browse')
if _BROWSE not in sys.path:
    sys.path.insert(0, _BROWSE)
import operator_browser_tabs as tabs

_staging = None


def staging():
    """Return (WSL-readable directory, Chrome-native path). Fixture override is explicit."""
    global _staging
    override = os.environ.get('OPERATOR_BROWSER_STAGING_DIR')
    if override:
        local = Path(override).resolve()
        native = os.environ.get('OPERATOR_BROWSER_STAGING_NATIVE', str(local))
    elif _staging:
        return _staging
    else:
        powershell = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
        temp = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-Command', '$env:TEMP'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        native = temp.rstrip('\\/') + '\\Operator\\files-v1'
        local = Path(subprocess.run(['wslpath', '-u', native], capture_output=True,
            text=True, check=True, timeout=3).stdout.strip()).resolve()
    local.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not override:
        _staging = local, native
    return local, native


def _guid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9-]{32,36}', value):
        raise ValueError('invalid transfer ID')
    return value


def stage_file(cid, fid):
    source, asset = workspace.file_path(cid, fid)
    local, native = staging()
    destination = local / 'uploads' / asset['id'] / asset['name']
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink():
        raise ValueError('invalid staging path')
    if not destination.exists():
        shutil.copyfile(source, destination)
    separator = '\\' if '\\' in native else '/'
    browser_path = separator.join([native, 'uploads', asset['id'], asset['name']])
    return {'file_id': fid, 'name': asset['name'], 'server_path': str(source),
            'browser_path': browser_path, 'owned_targets': tabs._read_registry().get(cid, [])}


def clean_upload(fid):
    # Only our exact UUID directory; never recurse through a caller path.
    if not re.fullmatch('[a-f0-9]{32}', fid):
        return
    try:
        local, _ = staging()
        folder = local / 'uploads' / fid
        if folder.is_symlink() or not folder.is_dir():
            return
        for child in folder.iterdir():
            if child.is_file() and not child.is_symlink():
                child.unlink()
        folder.rmdir()
    except (OSError, subprocess.SubprocessError):
        pass


async def upload_to_input(cid, fid, target_id, selector):
    if target_id not in tabs._read_registry().get(cid, []):
        raise ValueError('choose a browser target owned by this chat')
    if not isinstance(selector, str) or not selector or len(selector) > 500:
        raise ValueError('a file-input selector is required')
    staged = await asyncio.to_thread(stage_file, cid, fid)
    from playwright.async_api import async_playwright
    endpoint = os.environ.get('OPERATOR_DEMO_CDP') or 'http://127.0.0.1:9222'
    async with async_playwright() as pw:
        # A normal Playwright attach installs its own temporary download
        # directory. Only the persistent DownloadBridge owns that setting.
        browser = await pw.chromium.connect_over_cdp(endpoint, timeout=10000, no_defaults=True)
        try:
            for context in browser.contexts:
                for page in context.pages:
                    session = await context.new_cdp_session(page)
                    try:
                        info = await session.send('Target.getTargetInfo')
                        if info['targetInfo']['targetId'] != target_id:
                            continue
                        # DOM handles come from the current owned page, not a
                        # guessed remote node ID. CDP consumes a Windows path.
                        root = await session.send('DOM.getDocument')
                        found = await session.send('DOM.querySelector', {'nodeId': root['root']['nodeId'], 'selector': selector})
                        if not found.get('nodeId'):
                            raise ValueError('file input not found; inspect the current page')
                        await session.send('DOM.setFileInputFiles', {'nodeId': found['nodeId'], 'files': [staged['browser_path']]})
                        return {'file_id': fid, 'status': 'attached', 'target_id': target_id}
                    finally:
                        await session.detach()
            raise ValueError('owned browser tab is no longer available')
        finally:
            await browser.close()  # disconnect CDP, not Browser.close protocol


def clean_download(guid):
    try:
        local, _ = staging()
        for name in (_guid(guid), _guid(guid) + '.crdownload'):
            path = local / name
            if path.is_file() and not path.is_symlink():
                path.unlink()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


class DownloadBridge:
    def __init__(self, browser, local, native):
        self.browser, self.local, self.native = browser, local, native
        self.session = None
        self.lock = asyncio.Lock()
        self.tasks = set()

    @classmethod
    async def attach(cls, browser):
        local, native = await asyncio.to_thread(staging)
        bridge = cls(browser, local, native)
        bridge.session = await browser.new_browser_cdp_session()
        bridge.session.on('Browser.downloadWillBegin', lambda event: bridge.schedule('begin', event))
        bridge.session.on('Browser.downloadProgress', lambda event: bridge.schedule('progress', event))
        await bridge.configure()
        # Recover only transfers already attributed before a service reconnect.
        with workspace.database() as db:
            pending = [dict(r) for r in db.execute("SELECT * FROM transfers WHERE status='downloading'")]
        for row in pending:
            if (local / row['id']).is_file():
                bridge.schedule('progress', {'guid': row['id'], 'state': 'completed'})
        return bridge

    async def configure(self):
        await self.session.send('Browser.setDownloadBehavior', {
            'behavior': 'allowAndName', 'downloadPath': self.native, 'eventsEnabled': True})

    def schedule(self, kind, event):
        task = asyncio.create_task(self.handle(kind, event))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def owner(self, frame_id):
        owners = tabs._read_registry()
        direct = [cid for cid, targets in owners.items() if frame_id in targets]
        if len(direct) == 1:
            return direct[0]
        for context in self.browser.contexts:
            for page in context.pages:
                session = await context.new_cdp_session(page)
                try:
                    target = (await session.send('Target.getTargetInfo'))['targetInfo']['targetId']
                    candidates = [cid for cid, targets in owners.items() if target in targets]
                    if len(candidates) != 1:
                        continue
                    tree = (await session.send('Page.getFrameTree'))['frameTree']
                    stack = [tree]
                    while stack:
                        frame = stack.pop()
                        if frame['frame']['id'] == frame_id:
                            return candidates[0]
                        stack.extend(frame.get('childFrames', []))
                except Exception:
                    continue
                finally:
                    await session.detach()
        return ''

    async def handle(self, kind, event):
        async with self.lock:
            try:
                guid = _guid(event.get('guid'))
                if kind == 'begin':
                    cid = await asyncio.wait_for(self.owner(event.get('frameId')), timeout=5)
                    if not cid:
                        await self.session.send('Browser.cancelDownload', {'guid': guid})
                        return
                    with workspace.database() as db:
                        if db.execute('SELECT 1 FROM deleted_conversations WHERE id=?', (cid,)).fetchone():
                            await self.session.send('Browser.cancelDownload', {'guid': guid})
                            return
                        db.execute('INSERT OR IGNORE INTO transfers VALUES (?,?,?,?,?,?)',
                            (guid, cid, str(event.get('suggestedFilename') or 'download')[:240], 'downloading', 0, ''))
                    return
                with workspace.database() as db:
                    row = db.execute('SELECT * FROM transfers WHERE id=?', (guid,)).fetchone()
                    if not row:
                        return
                    asset = dict(row)
                if asset['status'] != 'downloading':
                    if asset['status'] == 'failed':
                        if event.get('state') == 'inProgress':
                            await self.session.send('Browser.cancelDownload', {'guid': guid})
                        await asyncio.to_thread(clean_download, guid)
                    return
                size = max(event.get('receivedBytes', 0), event.get('totalBytes', 0))
                state = workspace.snapshot(asset['conversation_id'])
                over_limit = (size > workspace.limit('FILE', 100)
                    or state['storage']['chat'] + size > workspace.limit('CHAT', 1024)
                    or state['storage']['total'] + size > workspace.limit('TOTAL', 10240))
                if over_limit or event.get('state') == 'canceled':
                    if over_limit:
                        await self.session.send('Browser.cancelDownload', {'guid': guid})
                    await asyncio.to_thread(self.failed, guid, 'Storage limit reached' if over_limit else 'Download canceled')
                    return
                if event.get('state') != 'completed':
                    return
                await asyncio.to_thread(self.complete, asset)
            except Exception:
                if event.get('guid'):
                    try: await asyncio.to_thread(self.failed, _guid(event['guid']), 'Download unavailable; try again')
                    except ValueError: pass

    def failed(self, guid, error):
        with workspace.database() as db:
            db.execute("UPDATE transfers SET status='failed',error=? WHERE id=?", (error, guid))
        clean_download(guid)
        import operator_diagnostics
        operator_diagnostics.record('file_transfer_failures')

    def complete(self, asset):
        path = self.local / _guid(asset['id'])
        if path.is_symlink() or not path.is_file():
            raise ValueError('completed download not available in staging')
        source = 'download:' + asset['id']
        with workspace.database() as db:
            old = db.execute('SELECT * FROM files WHERE conversation_id=? AND source=?', (asset['conversation_id'], source)).fetchone()
        if old:
            saved = dict(old)
        else:
            with path.open('rb') as stream:
                saved = workspace.put_file(asset['conversation_id'], asset['name'], stream, source=source)
        with workspace.database() as db:
            db.execute("UPDATE transfers SET status='ready',size=?,error='' WHERE id=?", (saved['size'], asset['id']))
        path.unlink()

    async def close(self):
        # Do not orphan an in-progress import when the capture connection turns
        # over. Pending Chrome downloads retain their durable ownership record.
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.session:
            try: await self.session.detach()
            except Exception: pass


def _background_work():
    with workspace.database() as db:
        return bool(db.execute("SELECT 1 FROM runs WHERE state='running' LIMIT 1").fetchone()
                    or db.execute("SELECT 1 FROM transfers WHERE status='downloading' LIMIT 1").fetchone())


class DownloadService:
    """One metadata-only CDP owner, independent of viewer/capture lifetime."""
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.last_use = 0
        self.reset = threading.Event()

    def ensure(self, reset=False):
        with self.lock:
            self.last_use = time.monotonic()
            if reset: self.reset.set()
            if self.thread and self.thread.is_alive(): return
            self.thread = threading.Thread(target=self.run, name='operator-downloads', daemon=True)
            self.thread.start()

    def run(self):
        try:
            asyncio.run(self.serve())
        finally:
            with self.lock:
                self.thread = None

    async def serve(self):
        from playwright.async_api import async_playwright
        endpoint = os.environ.get('OPERATOR_DEMO_CDP') or 'http://127.0.0.1:9222'
        async with async_playwright() as pw:
            while time.monotonic() - self.last_use < 60 or await asyncio.to_thread(_background_work):
                browser = manager = None
                try:
                    # No screenshot loop, page metrics or browser launch. All
                    # download policy belongs to this long-lived connection.
                    browser = await pw.chromium.connect_over_cdp(endpoint, timeout=5000, no_defaults=True)
                    manager = await asyncio.wait_for(DownloadBridge.attach(browser), 8)
                    next_idle_check = 0
                    while browser.is_connected():
                        if self.reset.is_set():
                            self.reset.clear()
                            await manager.configure()
                        now = time.monotonic()
                        if now - self.last_use >= 60 and now >= next_idle_check:
                            if not await asyncio.to_thread(_background_work): return
                            next_idle_check = now + 10
                        await asyncio.sleep(.5)
                except Exception:
                    import operator_diagnostics
                    operator_diagnostics.record('file_transfer_failures')
                finally:
                    if manager:
                        try: await asyncio.wait_for(manager.close(), 3)
                        except Exception: pass
                    if browser:
                        try: await browser.close()
                        except Exception: pass
                await asyncio.sleep(5)


_service = DownloadService()


def ensure_running(reset=False):
    if os.environ.get('OPERATOR_DEMO') != '1' and os.environ.get('OPERATOR_FILE_BRIDGE', '1') != '0':
        _service.ensure(reset)
