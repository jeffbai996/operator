import asyncio
import io
import sys
from types import ModuleType, SimpleNamespace

import pytest

import operator_file_bridge as bridge
import operator_workspace as ws


def test_upload_staging_keeps_name_and_checks_chat_ownership(monkeypatch):
    file = ws.put_file('a', 'example.txt', io.BytesIO(b'fixture'))
    monkeypatch.setenv('OPERATOR_BROWSER_STAGING_NATIVE', 'C:\\Temp\\Operator\\fixture')
    staged = bridge.stage_file('a', file['id'])
    assert staged['browser_path'].startswith('C:\\Temp\\Operator\\fixture\\uploads\\')
    assert staged['browser_path'].endswith('\\example.txt')
    with pytest.raises(KeyError):
        bridge.stage_file('b', file['id'])
    ws.delete_file('a', file['id'])
    assert not (bridge.staging()[0] / 'uploads' / file['id']).exists()


def test_download_completion_is_owned_and_idempotent():
    local, native = bridge.staging()
    manager = bridge.DownloadBridge(None, local, native)
    guid = 'd' * 32
    with ws.database() as db:
        db.execute('INSERT INTO transfers VALUES (?,?,?,?,?,?)', (guid, 'a', 'receipt.txt', 'downloading', 0, ''))
    (local / guid).write_bytes(b'completed receipt')
    asset = {'id': guid, 'conversation_id': 'a', 'name': 'receipt.txt'}
    manager.complete(asset)
    assert ws.snapshot('a')['files'][0]['name'] == 'receipt.txt'
    assert ws.snapshot('b')['files'] == []
    # A reconnect after import but before cleanup must not import twice.
    (local / guid).write_bytes(b'completed receipt')
    manager.complete(asset)
    assert len(ws.snapshot('a')['files']) == 1


def test_partial_or_deleted_chat_download_cannot_become_ready():
    local, native = bridge.staging()
    manager = bridge.DownloadBridge(None, local, native)
    guid = 'e' * 32
    (local / (guid + '.crdownload')).write_bytes(b'partial')
    with pytest.raises(ValueError):
        manager.complete({'id': guid, 'conversation_id': 'a', 'name': 'file'})
    ws.delete_conversation('a')
    (local / guid).write_bytes(b'late completion')
    with pytest.raises(ws.Conflict):
        manager.complete({'id': guid, 'conversation_id': 'a', 'name': 'file'})
    assert ws.snapshot('a')['files'] == []


def test_unknown_download_is_cancelled_not_assigned_to_visible_chat():
    local, native = bridge.staging()
    manager = bridge.DownloadBridge(SimpleNamespace(contexts=[]), local, native)
    calls = []
    async def send(method, params):
        calls.append((method, params))
    manager.session = SimpleNamespace(send=send)
    asyncio.run(manager.handle('begin', {'guid': 'f' * 32, 'frameId': 'unknown', 'suggestedFilename': 'x'}))
    assert calls == [('Browser.cancelDownload', {'guid': 'f' * 32})]


def test_upload_refuses_foreign_target_before_connecting(monkeypatch):
    monkeypatch.setattr(bridge.tabs, '_read_registry', lambda: {'a': ['owned'], 'b': ['foreign']})
    with pytest.raises(ValueError, match='owned'):
        asyncio.run(bridge.upload_to_input('a', 'file', 'foreign', 'input[type=file]'))


def test_generated_file_publish_cannot_escape_artifact_directory(monkeypatch, tmp_path):
    import operator_job_tools as tools
    run = ws.start_run('a', 'Make a file')
    monkeypatch.setenv('OPERATOR_RUN_ID', run['run_id'])
    monkeypatch.setenv('OPERATOR_RUN_CREDENTIAL', run['credential'])
    private = tmp_path / 'outside'; private.write_text('not an artifact')
    with pytest.raises(ValueError):
        tools.call('job_file', {'action': 'publish', 'path': str(private)})
    directory = ws.root() / 'artifacts' / run['run_id']
    (directory / 'result.txt').write_text('result')
    tools.call('job_file', {'action': 'publish', 'path': 'result.txt'})
    assert ws.snapshot('a')['files'][0]['name'] == 'result.txt'


def test_download_service_survives_viewer_idle_without_capture(monkeypatch):
    from unittest.mock import AsyncMock
    service = bridge.DownloadService()
    service.last_use = 0
    service.reset.set()
    manager = SimpleNamespace(configure=AsyncMock(), close=AsyncMock())
    browser = SimpleNamespace(is_connected=lambda: True, close=AsyncMock())
    connect = AsyncMock(return_value=browser)
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
    class Context:
        async def __aenter__(self): return pw
        async def __aexit__(self, *args): pass
    # The service imports Playwright lazily. Supply that optional boundary
    # directly so this unit test checks lifecycle behavior without requiring
    # a browser automation installation in the repository-wide test venv.
    playwright = ModuleType("playwright")
    playwright.__path__ = []
    api = ModuleType("playwright.async_api")
    api.async_playwright = Context
    playwright.async_api = api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    work = iter([True, False])  # background job keeps it alive, then finishes
    monkeypatch.setattr(bridge, '_background_work', lambda: next(work))
    monkeypatch.setattr(bridge.DownloadBridge, 'attach', AsyncMock(return_value=manager))
    asyncio.run(service.serve())
    assert connect.call_args.kwargs['no_defaults'] is True
    manager.configure.assert_awaited_once()
    manager.close.assert_awaited_once()
