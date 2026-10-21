"""Infrastructure browser ownership is separate from the human cockpit."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import operator_session as sessions
import operator_view as view


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, '_PATH', str(tmp_path / 'sessions.json'))
    return sessions


def test_machine_only_history_never_becomes_the_human_active_chat(store):
    cid = store.create('job', activate=False, reuse_draft=False, origin='app')['id']
    assert store.active_id() == ''
    assert cid in store.background_ids()


def test_inspect_and_return_preserve_the_job_and_human_history(store):
    human = store.create('human')['id']
    job = store.create('job', activate=False, reuse_draft=False, origin='mcp')['id']
    store.set_browser_exposed(job, True)
    assert job not in store.background_ids()
    assert store.active_id() == human
    store.activate(job)
    store.set_browser_exposed(job, False)
    assert job in store.background_ids()
    assert store.active_id() == human
    assert store.exists(job)


def test_background_pages_are_absent_from_the_picker_and_page_selection(monkeypatch):
    human, job = SimpleNamespace(url='https://example.org'), SimpleNamespace(url='https://example.test')
    streamer = view._Streamer()
    monkeypatch.setattr(view, '_background_targets', lambda: {'JOB'})
    monkeypatch.setattr(streamer, '_live_pages', lambda ctx: [human, job])
    async def target(page):
        return 'JOB' if page is job else 'HUMAN'
    monkeypatch.setattr(streamer, '_page_target_id', target)
    assert asyncio.run(streamer._cockpit_pages(None)) == [human]


def test_machine_snapshots_do_not_start_or_renew_the_cockpit(monkeypatch):
    import operator_browser_jobs as jobs
    monkeypatch.setattr(view._streamer, 'require_ready', lambda: pytest.fail('started cockpit'))
    monkeypatch.setattr(jobs, 'look', lambda cid, endpoint: {
        'jpeg_b64': '/9hqcGVn', 'url': 'https://example.test', 'title': 'Job', 'cached': True})
    out, code = view.look_for_machine({'conversation_id': 'job'})
    assert code == 200 and out['cached'] and out['conversation_id'] == 'job'


def test_background_reservation_creates_a_minimized_window_in_the_same_profile(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('bg_tabs',
        Path(__file__).resolve().parents[2] / 'browse/operator_browser_tabs.py')
    tabs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tabs)
    calls = []
    monkeypatch.setattr(tabs, '_page_targets', lambda endpoint: {'manual'})
    monkeypatch.setattr(tabs.TabCleanupClient, '_get', lambda self, suffix: {'webSocketDebuggerUrl': 'ws://browser'})
    def rpc(self, url, method, params=None):
        calls.append((method, params))
        return {'targetId': 'JOB', 'windowId': 1, 'bounds': {'windowState': 'minimized'}}
    monkeypatch.setattr(tabs.TabCleanupClient, '_rpc', rpc)
    path = tmp_path / 'tabs.json'
    assert tabs.reserve('job', 'http://browser', path, background=True) == 'JOB'
    method, params = calls[0]
    assert method == 'Target.createTarget'
    assert params['newWindow'] and params['background']
    assert params['windowState'] == 'minimized'
    assert 'browserContextId' not in params  # the existing cookies/extensions
    assert tabs._read_registry(path) == {'job': ['JOB']}


def test_cleanup_waits_for_approval_and_human_takeover(store, monkeypatch):
    import operator_browser_jobs as jobs
    import operator_workspace as workspace
    cid = store.create('job', activate=False, reuse_draft=False, origin='app')['id']
    monkeypatch.setattr(workspace, 'snapshot', lambda cid: {'jobs': [{'state': 'needs_input'}]})
    assert not jobs.can_release(cid)
    monkeypatch.setattr(workspace, 'snapshot', lambda cid: {'jobs': []})
    assert jobs.can_release(cid)
    store.set_browser_exposed(cid, True)
    assert not jobs.can_release(cid)


def test_cached_frame_is_available_after_tabs_and_browser_are_gone(tmp_path, monkeypatch):
    import operator_browser_jobs as jobs
    helper = jobs.tabs()
    monkeypatch.setattr(helper, 'DEFAULT_REGISTRY', tmp_path / 'tabs.json')
    monkeypatch.setattr(helper, '_read_registry', lambda path=None: {})
    monkeypatch.setattr(helper, 'TabCleanupClient', lambda *a, **kw: pytest.fail('contacted browser'))
    jobs._save_capture('job', {'jpeg_b64': '/9hqcGVn', 'url': 'https://example.test'})
    assert jobs.look('job', 'http://offline')['cached']
    jobs.clear_capture('job')
    assert jobs.look('job', 'http://offline') is None


@pytest.mark.parametrize('reason', ['visible', 'shared', 'failed_close', 'success'])
def test_finished_job_cleanup_preserves_manual_tabs_and_failed_ownership(tmp_path, monkeypatch, reason):
    import operator_browser_jobs as jobs
    helper = jobs.tabs()
    path = tmp_path / 'tabs.json'
    monkeypatch.setattr(helper, 'DEFAULT_REGISTRY', path)
    registry = {'job': ['owned']}
    if reason == 'shared':
        registry['human'] = ['owned']
    helper._write_registry(registry, path)
    rows = {'owned': {'id': 'owned', 'type': 'page'}, 'manual': {'id': 'manual', 'type': 'page'}}
    closed = []
    class Client:
        def _get(self, suffix):
            return {'webSocketDebuggerUrl': 'ws://browser'} if suffix == '/json/version' else list(rows.values())
        def _rpc(self, ws, method, params):
            return {'bounds': {'windowState': 'normal' if reason == 'visible' else 'minimized'}}
        def close(self, target):
            if reason == 'failed_close':
                raise OSError('offline')
            closed.append(target)
            rows.pop(target)
    monkeypatch.setattr(helper, 'TabCleanupClient', lambda *a, **kw: Client())
    if reason == 'failed_close':
        with pytest.raises(OSError):
            jobs.release('job', 'http://browser')
    else:
        assert jobs.release('job', 'http://browser') == (1 if reason == 'success' else 0)
    assert 'manual' in rows
    assert closed == (['owned'] if reason == 'success' else [])
    assert helper._read_registry(path) == ({} if reason == 'success' else registry)


def test_new_dispatch_invalidates_the_finished_job_cleanup_lease(store):
    import operator_agent
    job = store.create('job', origin='mcp', activate=False, reuse_draft=False)['id']
    pool = operator_agent.RunnerRegistry()
    fake = SimpleNamespace(state='done', handoff=None, _thread=None, started_ts=1,
                           ended_ts=2, is_running=lambda: False)
    pool._runners[job] = fake
    generation = pool.background_cleanup_candidates()[job]
    fake.state = 'running'
    assert not pool.with_background_cleanup_lease(job, generation,
                                                 lambda: pytest.fail('closed a live run'))


def test_rejected_admission_never_prepares_or_launches_a_browser(store):
    import operator_agent
    pool = operator_agent.RunnerRegistry()
    out = pool.start('unknown-bot', 'task', prepare_browser=lambda cid: pytest.fail('launched browser'))
    assert not out['ok']


def _fake_tab_helper(calls, *, fail=False):
    class Client:
        def __init__(self, endpoint, timeout=1.0):
            calls.append(('client', endpoint, timeout))

        def _get(self, suffix):
            return [{'id': 'other', 'webSocketDebuggerUrl': 'ws://other'},
                    {'id': 'mine', 'webSocketDebuggerUrl': 'ws://mine'}]

        def _rpc(self, url, method, params=None):
            calls.append(('rpc', url, method, params))
            return {}

    def reserve(cid, endpoint, path, **kwargs):
        if fail:
            raise RuntimeError('chrome is down')
        calls.append(('reserve', cid, endpoint, kwargs))
        return 'mine'
    return SimpleNamespace(reserve=reserve, DEFAULT_REGISTRY='registry', TabCleanupClient=Client)


def test_a_start_page_opens_on_the_conversations_own_target(monkeypatch):
    import operator_browser_jobs as jobs
    calls = []
    monkeypatch.setattr(jobs, 'tabs', lambda: _fake_tab_helper(calls))
    ensured = []
    assert jobs.open_start_page('conv-1', 'http://cdp', 'example.com/x',
                                lambda: ensured.append(1)) is None
    assert ensured == [1]
    assert ('reserve', 'conv-1', 'http://cdp', {}) in calls
    assert ('rpc', 'ws://mine', 'Page.navigate', {'url': 'https://example.com/x'}) in calls
    assert not any(c[0] == 'rpc' and c[1] == 'ws://other' for c in calls)


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'chrome://settings', 'data://text/html,x'])
def test_a_start_page_must_be_a_web_address(monkeypatch, url):
    import operator_browser_jobs as jobs
    calls = []
    monkeypatch.setattr(jobs, 'tabs', lambda: _fake_tab_helper(calls))
    error = jobs.open_start_page('conv-1', 'http://cdp', url, lambda: pytest.fail('touched Chrome'))
    assert error == "the task's start page must be an http or https address"
    assert calls == []


def test_a_start_page_failure_is_reported_not_raised(monkeypatch):
    import operator_browser_jobs as jobs
    monkeypatch.setattr(jobs, 'tabs', lambda: _fake_tab_helper([], fail=True))
    assert jobs.open_start_page('conv-1', 'http://cdp', 'https://example.com',
                                lambda: None) == "the task's start page could not be opened"


def test_admission_hands_preparation_the_admitted_conversation(store):
    import operator_agent
    seen = []

    class Inert:
        def __init__(self, conversation_id, **_):
            self.running = False

        def is_running(self):
            return self.running

        def start(self, bot, task, **_):
            self.running = True
            return {'ok': True}
    pool = operator_agent.RunnerRegistry(runner_factory=Inert)
    assert pool.start('claude-a', 'task', conversation_id='conv 1',
                      prepare_browser=lambda cid: seen.append(cid))['ok']
    assert seen == ['conv-1']


def test_real_chromium_background_window_shares_cookies_and_keeps_manual_window(tmp_path, monkeypatch):
    """Disposable Chromium and profile; never attach to the user's browser."""
    import os
    import shutil
    import tempfile
    import subprocess
    import socket
    import secrets
    import time
    import operator_browser_jobs as jobs
    monkeypatch.setattr(sessions, 'is_background', lambda cid: True)
    pw = pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as playwright:
        executable = os.environ.get('CC_TEST_CHROMIUM') or playwright.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip('managed Chromium is unavailable')
        scratch = '/mnt/wsl-storage/scratch' if Path('/mnt/wsl-storage/scratch').is_dir() else str(tmp_path)
        if not shutil.which('Xvfb') or not shutil.which('openbox') or not shutil.which('xprop'):
            pytest.skip('headed isolation check requires Xvfb and a window manager')
        profile = Path(tempfile.mkdtemp(prefix='op-background-chrome-', dir=scratch))
        context = None
        # WSLg mounts /tmp/.X11-unix read-only. A fixed unused abstract Unix
        # socket with -pn works there; Xvfb's dynamic -displayfd insists that
        # every filesystem transport bind and loops forever on that mount.
        display_number = 1000 + secrets.randbelow(10000)
        display = ':' + str(display_number)
        display_log = (tmp_path / 'display.log').open('w+')
        xvfb = subprocess.Popen(['Xvfb', display, '-screen', '0', '1600x1000x24', '-nolisten', 'tcp', '-pn'],
                                stdout=subprocess.DEVNULL, stderr=display_log, text=True)
        wm = None
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                probe = socket.socket(socket.AF_UNIX)
                try:
                    probe.connect('\0/tmp/.X11-unix/X' + str(display_number))
                    break
                except OSError:
                    time.sleep(.05)
                finally:
                    probe.close()
            else:
                display_log.seek(0)
                pytest.fail('scratch display did not start: ' + display_log.read()[-2000:])
            desktop_env = {**os.environ, 'DISPLAY': display}
            wm = subprocess.Popen(['openbox'], env=desktop_env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = subprocess.run(['xprop', '-root', '_NET_SUPPORTING_WM_CHECK'],
                    env=desktop_env, capture_output=True, text=True, timeout=2)
                if 'window id' in result.stdout:
                    break
                time.sleep(.05)
            else:
                pytest.fail('scratch window manager did not start')
            context = playwright.chromium.launch_persistent_context(str(profile),
                executable_path=executable, headless=False, env=desktop_env,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--remote-debugging-port=0'])
            # Playwright is connected over its pipe before Chrome necessarily
            # publishes the separate CDP port file used by the real MCP client.
            port_file = profile / 'DevToolsActivePort'
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    lines = port_file.read_text().splitlines()
                    if len(lines) >= 2 and lines[0].isdigit():
                        port = int(lines[0])
                        break
                except FileNotFoundError:
                    pass
                time.sleep(.05)
            else:
                pytest.fail('scratch Chromium did not publish its CDP port')
            endpoint = f'http://127.0.0.1:{port}'
            helper = jobs.tabs()
            path = tmp_path / 'tabs.json'
            monkeypatch.setattr(helper, 'DEFAULT_REGISTRY', path)
            client = helper.TabCleanupClient(endpoint, timeout=5)
            ws = client._get('/json/version')['webSocketDebuggerUrl']
            manual = next(row for row in client._get('/json/list') if row['type'] == 'page')
            original = client._rpc(ws, 'Browser.getWindowForTarget', {'targetId': manual['id']})
            context.add_cookies([{'name': 'signed_in', 'value': 'yes', 'url': 'https://example.test'}])
            owned = helper.reserve('job', endpoint, path, background=True)
            window = client._rpc(ws, 'Browser.getWindowForTarget', {'targetId': owned})
            assert window['windowId'] != original['windowId']
            # Native window state changes are asynchronous to CDP commands.
            deadline = time.monotonic() + 5
            while window['bounds']['windowState'] != 'minimized' and time.monotonic() < deadline:
                time.sleep(.05)
                window = client._rpc(ws, 'Browser.getWindowForTarget', {'targetId': owned})
            assert window['bounds']['windowState'] == 'minimized'
            row = next(r for r in client._get('/json/list') if r['id'] == owned)
            cookies = client._rpc(row['webSocketDebuggerUrl'], 'Network.getCookies',
                                  {'urls': ['https://example.test']})['cookies']
            assert any(cookie['name'] == 'signed_in' for cookie in cookies)
            # Exercise the real MCP SDK, not just the CDP reservation helper.
            import json
            import select
            import sys
            browse = Path(__file__).resolve().parents[2] / 'browse'
            common = subprocess.run(['git', 'rev-parse', '--git-common-dir'], cwd=browse,
                                    capture_output=True, text=True, check=True).stdout.strip()
            sdk = (browse / common).resolve().parent / 'modules/browse/node_modules'
            assert (sdk / '@playwright/mcp/index.js').is_file()
            process = subprocess.Popen(['node', str(browse / 'operator_playwright_mcp.js')],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, env={**os.environ, 'OPERATOR_BACKGROUND_BROWSER': '1',
                    'OPERATOR_BACKGROUND_LAZY': '0', 'OPERATOR_CDP_ENDPOINT': endpoint,
                    'OPERATOR_BROWSER_TARGET_ID': owned, 'OPERATOR_CONVERSATION_ID': 'job',
                    'OPERATOR_BROWSER_TABS_PATH': str(path),
                    'OPERATOR_BROWSER_TABS_PYTHON': sys.executable,
                    'OPERATOR_PLAYWRIGHT_NODE_MODULES': str(sdk)})
            def request(message):
                process.stdin.write(json.dumps({'jsonrpc': '2.0', **message}) + '\n')
                process.stdin.flush()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                        line = process.stdout.readline()
                        assert line, 'MCP exited before replying'
                        reply = json.loads(line)
                        if reply.get('id') == message.get('id'):
                            return reply
                pytest.fail('MCP browser tool did not reply')
            try:
                assert request({'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': '2024-11-05', 'capabilities': {},
                    'clientInfo': {'name': 'background-check', 'version': '1'}}}).get('result')
                process.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
                process.stdin.flush()
                reply = request({'id': 2, 'method': 'tools/call', 'params': {
                    'name': 'browser_tabs', 'arguments': {'action': 'new'}}})
                assert not reply.get('error') and not reply['result'].get('isError'), reply
                extra = set(helper._read_registry(path)['job']) - {owned}
                assert len(extra) == 1
                for target in extra:
                    info = client._rpc(ws, 'Browser.getWindowForTarget', {'targetId': target})
                    assert info['windowId'] != original['windowId']
                    assert info['bounds']['windowState'] == 'minimized'
                    client.close(target)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process.stdin.close()
                process.stdout.close()
            shot = jobs.look('job', endpoint)
            assert shot and shot['jpeg_b64'] and not shot['cached']
            helper.activate('job', endpoint, path, background=True)
            assert client._rpc(ws, 'Browser.getWindowForTarget', {'targetId': owned})['bounds']['windowState'] == 'normal'
            assert jobs.release('job', endpoint) == 0
            assert jobs.park('job', endpoint)
            assert jobs.release('job', endpoint) == 1
            assert manual['id'] in {r['id'] for r in client._get('/json/list')}
            assert jobs.look('job', endpoint)['cached']
        finally:
            if context:
                context.close()
            for proc in (wm, xvfb):
                if proc:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
            display_log.close()
            shutil.rmtree(profile, ignore_errors=True)


def test_normal_idle_reaper_defers_machine_chats_to_background_cleanup(store):
    import operator_tab_cleanup as cleanup
    cid = store.create('waiting job', activate=False, reuse_draft=False, origin='app')['id']
    runner = SimpleNamespace(tab_cleanup_candidates=lambda: {cid: ('done', 1)},
                             with_tab_cleanup_lease=lambda *args: pytest.fail('normal reaper claimed job'))
    reaper = SimpleNamespace(sweep=lambda client, candidates, guard: {'candidates': candidates})
    fake_view = SimpleNamespace(operator_agent=SimpleNamespace(runner=runner),
                                _streamer=SimpleNamespace(last_view=0),
                                _desktop_feed=SimpleNamespace(last_view=0))
    assert cleanup.sweep(fake_view, reaper, None)['candidates'] == {}


@pytest.mark.parametrize('background,minimized,retained', [(True, True, True), (False, True, False), (True, False, False)])
def test_cached_captures_exclude_human_controlled_windows(tmp_path, monkeypatch, background, minimized, retained):
    import operator_browser_jobs as jobs
    helper = jobs.tabs()
    monkeypatch.setattr(helper, 'DEFAULT_REGISTRY', tmp_path / 'tabs.json')
    helper._write_registry({'job': ['PAGE']}, helper.DEFAULT_REGISTRY)
    monkeypatch.setattr(sessions, 'is_background', lambda cid: background)
    class Client:
        def _get(self, suffix):
            if suffix == '/json/version':
                return {'webSocketDebuggerUrl': 'ws://browser'}
            return [{'id': 'PAGE', 'type': 'page', 'webSocketDebuggerUrl': 'ws://page'}]
        def _rpc(self, ws, method, params=None):
            return {'data': '/9hqcGVn'} if method == 'Page.captureScreenshot' else {
                'bounds': {'windowState': 'minimized' if minimized else 'normal'}}
    monkeypatch.setattr(helper, 'TabCleanupClient', lambda *args, **kw: Client())
    assert jobs.look('job', 'http://browser')['jpeg_b64']
    assert jobs._capture_path('job').exists() == retained


def test_returning_the_last_visible_job_clears_the_cockpit_frame(monkeypatch):
    streamer = view._Streamer()
    streamer._browser = SimpleNamespace(contexts=[None])
    streamer._page = SimpleNamespace(is_closed=lambda: False)
    streamer.frame = b'old job image'
    async def empty(ctx):
        return []
    monkeypatch.setattr(streamer, '_cockpit_pages', empty)
    asyncio.run(streamer._refresh_active_page())
    assert streamer._page is None and streamer.frame is None


def test_active_download_keeps_a_finished_job_browser(store, monkeypatch):
    import operator_browser_jobs as jobs
    import operator_workspace as workspace
    cid = store.create('job', activate=False, reuse_draft=False, origin='app')['id']
    monkeypatch.setattr(workspace, 'snapshot', lambda cid: {'jobs': [], 'transfers': [{'status': 'downloading'}]})
    assert not jobs.can_release(cid)
