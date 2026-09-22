"""Opt-in, quota-free Windows Chrome / WSL integration fixture.

Uses its own headless Chrome, temporary profile and port 0. Never attaches to
Operator's :9222, bots' :9224, or any signed-in profile. No external websites.
"""
import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import pytest

import operator_file_bridge as bridge
import operator_workspace as ws


@pytest.mark.skipif(os.environ.get('OPERATOR_TEST_WINDOWS_FILES') != '1', reason='explicit isolated Windows fixture only')
def test_windows_upload_and_completed_download(monkeypatch):
    chrome = '/mnt/c/Program Files/Google/Chrome/Application/chrome.exe'
    if not Path(chrome).is_file():
        pytest.fail('Windows Chrome is required for the requested fixture')
    powershell = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
    win_temp = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-Command', '$env:TEMP'],
        capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    temp = Path(subprocess.run(['wslpath', '-u', win_temp], capture_output=True, text=True,
                               check=True, timeout=3).stdout.strip()).resolve()
    directory = Path(tempfile.mkdtemp(prefix='operator-file-fixture-', dir=temp)).resolve()
    profile = directory / 'profile'; profile.mkdir()
    native_profile = subprocess.run(['wslpath', '-w', str(profile)], capture_output=True,
        text=True, check=True, timeout=3).stdout.strip()
    staging = directory / 'staging'; staging.mkdir()
    native_staging = subprocess.run(['wslpath', '-w', str(staging)], capture_output=True,
        text=True, check=True, timeout=3).stdout.strip()
    monkeypatch.setenv('OPERATOR_BROWSER_STAGING_DIR', str(staging))
    monkeypatch.setenv('OPERATOR_BROWSER_STAGING_NATIVE', native_staging)
    process = subprocess.Popen([chrome, '--headless=new', '--no-first-run', '--no-default-browser-check',
        '--remote-debugging-port=0', '--user-data-dir=' + native_profile, 'about:blank'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def exercise(endpoint):
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(endpoint, timeout=10000)
            session = await browser.new_browser_cdp_session()
            manager = None
            try:
                page = browser.contexts[0].pages[0]
                target_session = await page.context.new_cdp_session(page)
                target = (await target_session.send('Target.getTargetInfo'))['targetInfo']['targetId']
                await target_session.detach()
                monkeypatch.setattr(bridge.tabs, '_read_registry', lambda: {'fixture-chat': [target]})
                manager = await bridge.DownloadBridge.attach(browser)
                progress = []
                failures = []
                complete = manager.complete
                def observed_complete(asset):
                    try:
                        return complete(asset)
                    except Exception as exc:
                        failures.append(str(exc))
                        raise
                manager.complete = observed_complete
                manager.session.on('Browser.downloadProgress', lambda event: progress.append(event))
                await page.set_content('<input type="file" id="upload"><a id="download" download="receipt.txt">Download</a>')
                source = ws.put_file('fixture-chat', 'upload.txt', io.BytesIO(b'Windows upload fixture'))
                await bridge.upload_to_input('fixture-chat', source['id'], target, '#upload')
                assert await page.locator('#upload').evaluate('(e) => e.files[0].text()') == 'Windows upload fixture'
                await page.locator('#download').evaluate('(e) => e.href = URL.createObjectURL(new Blob(["Windows download fixture"], {type:"text/plain"}))')
                await page.locator('#download').click()
                deadline = time.monotonic() + 10
                files = []
                while time.monotonic() < deadline:
                    files = ws.snapshot('fixture-chat')['files']
                    if len(files) == 2:
                        break
                    await asyncio.sleep(.1)
                if len(files) != 2:
                    pytest.fail(json.dumps({'transfers': ws.snapshot('fixture-chat')['transfers'],
                        'progress': progress, 'staged': [p.name for p in staging.iterdir()], 'failures': failures}))
                receipt = next(f for f in files if f['name'] == 'receipt.txt')
                assert ws.file_path('fixture-chat', receipt['id'])[0].read_bytes() == b'Windows download fixture'
                assert ws.snapshot('another-chat')['files'] == []
            finally:
                if manager:
                    await manager.close()
                # This connection was created from our freshly minted profile's
                # DevToolsActivePort, never a shared Chrome endpoint.
                try: await session.send('Browser.close')
                except Exception: pass
                await browser.close()

    try:
        port_file = profile / 'DevToolsActivePort'
        deadline = time.monotonic() + 12
        while not port_file.is_file() and time.monotonic() < deadline:
            time.sleep(.1)
        assert port_file.is_file(), 'isolated Windows Chrome did not start'
        port = int(port_file.read_text().splitlines()[0])
        assert port not in (9222, 9224)
        endpoint = f'http://127.0.0.1:{port}'
        monkeypatch.setenv('OPERATOR_DEMO_CDP', endpoint)
        asyncio.run(exercise(endpoint))
    finally:
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.terminate()
        # Resolve and validate the exact newly-created fixture directory before
        # recursive cleanup. It contains no signed-in user/browser data.
        assert directory.parent == temp and directory.name.startswith('operator-file-fixture-')
        for _ in range(20):
            try:
                shutil.rmtree(directory)
                break
            except OSError:
                time.sleep(.1)
