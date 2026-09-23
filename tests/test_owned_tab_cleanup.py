"""Automatic cleanup must never infer ownership from a browser's tab order."""
import importlib.util
from pathlib import Path

import pytest
import types

_spec = importlib.util.spec_from_file_location(
    "owned_tab_helper", Path(__file__).resolve().parents[1] / "browse" / "operator_browser_tabs.py")
BT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(BT)


class Browser:
    def __init__(self):
        self.rows = {"owned": {"id": "owned", "url": "https://example.test", "attached": False},
                     "manual": {"id": "manual", "url": "https://example.org", "attached": False}}
        self.safe = True
        self.closed = []
        self.fail = False

    def pages(self):
        return self.rows.copy()

    def idle_page(self, row):
        return self.safe

    def close(self, target):
        if self.fail:
            raise OSError("offline")
        self.closed.append(target)
        self.rows.pop(target)


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "tabs.json"
    BT._write_registry({"done": ["owned"]}, path)
    browser = Browser()
    reaper = BT.IdleTabReaper(path=path, idle_seconds=60)
    return path, browser, reaper


def sweep(reaper, browser, now, candidates=None, guard=None):
    return reaper.sweep(browser, {"done": 1} if candidates is None else candidates,
                        guard or (lambda cid, generation, action: action()), now=now)


def test_only_owned_finished_hidden_tabs_expire_after_full_grace(setup):
    path, browser, reaper = setup
    sweep(reaper, browser, 0)
    sweep(reaper, browser, 59)
    assert browser.closed == []
    sweep(reaper, browser, 60)
    assert browser.closed == ["owned"]
    assert set(browser.rows) == {"manual"}
    assert BT._read_registry(path) == {}


@pytest.mark.parametrize("reason", ["visible_or_edited", "attached", "unknown_runner"])
def test_active_or_uncertain_tabs_are_preserved(setup, reason):
    path, browser, reaper = setup
    browser.safe = reason != "visible_or_edited"
    browser.rows["owned"]["attached"] = reason == "attached"
    candidates = {} if reason == "unknown_runner" else {"done": 1}
    sweep(reaper, browser, 0, candidates)
    sweep(reaper, browser, 120, candidates)
    assert browser.closed == []
    assert BT._read_registry(path) == {"done": ["owned"]}


@pytest.mark.parametrize("change", ["navigation", "new_run", "viewing", "restarted_worker"])
def test_activity_or_restart_restarts_grace(setup, change):
    path, browser, reaper = setup
    sweep(reaper, browser, 0)
    candidates = {"done": 1}
    if change == "navigation":
        browser.rows["owned"]["url"] += "/new"
    elif change == "new_run":
        candidates = {"done": 2}
    elif change == "viewing":
        reaper.reset()
    else:
        reaper = BT.IdleTabReaper(path=path, idle_seconds=60)
    sweep(reaper, browser, 61, candidates)
    assert browser.closed == []
    sweep(reaper, browser, 121, candidates)
    assert browser.closed == ["owned"]


def test_close_failure_retains_ownership_for_retry(setup):
    path, browser, reaper = setup
    sweep(reaper, browser, 0)
    browser.fail = True
    sweep(reaper, browser, 61)
    assert BT._read_registry(path) == {"done": ["owned"]}
    browser.fail = False
    sweep(reaper, browser, 122)
    sweep(reaper, browser, 182)
    assert browser.closed == ["owned"]


def test_new_dispatch_guard_can_veto_the_close(setup):
    path, browser, reaper = setup
    sweep(reaper, browser, 0)
    sweep(reaper, browser, 61, guard=lambda *args: False)
    assert browser.closed == []
    assert BT._read_registry(path) == {"done": ["owned"]}


def test_accepted_but_uncompleted_close_retains_ownership(setup):
    path, browser, reaper = setup
    browser.close = lambda target: None
    sweep(reaper, browser, 0)
    sweep(reaper, browser, 61)
    assert BT._read_registry(path) == {"done": ["owned"]}


def test_final_page_is_never_closed(setup):
    path, browser, reaper = setup
    browser.rows.pop("manual")
    sweep(reaper, browser, 0)
    sweep(reaper, browser, 61)
    assert browser.closed == []


def test_multiple_owners_are_ambiguous_not_permission_to_close(setup):
    path, browser, reaper = setup
    BT._write_registry({"done": ["owned"], "other": ["owned"]}, path)
    sweep(reaper, browser, 0)
    sweep(reaper, browser, 61)
    assert browser.closed == []


def test_snapshot_failure_resets_idle_proof(setup):
    path, browser, reaper = setup
    sweep(reaper, browser, 0)
    original = browser.pages
    browser.pages = lambda: (_ for _ in ()).throw(OSError("offline"))
    sweep(reaper, browser, 61)
    browser.pages = original
    sweep(reaper, browser, 122)
    assert browser.closed == []


def test_stale_registry_entries_are_pruned_without_closing_anything(setup):
    path, browser, reaper = setup
    browser.rows.pop("owned")
    sweep(reaper, browser, 0)
    assert BT._read_registry(path) == {}
    assert browser.closed == []


def test_worker_pauses_for_viewers_and_controller_presence(setup, monkeypatch):
    import operator_session
    import operator_tab_cleanup as worker
    import time
    path, browser, reaper = setup
    lease = [0]
    monkeypatch.setattr(operator_session, "presence", lambda cid: {"lease_expires_in": lease[0]})
    runner = types.SimpleNamespace(
        tab_cleanup_candidates=lambda: {"done": 1},
        with_tab_cleanup_lease=lambda cid, generation, action: action())
    view = types.SimpleNamespace(operator_agent=types.SimpleNamespace(runner=runner),
        _streamer=types.SimpleNamespace(last_view=time.monotonic()),
        _desktop_feed=types.SimpleNamespace(last_view=0))
    assert worker.sweep(view, reaper, browser)['paused'] == 'viewer'
    view._streamer.last_view = 0
    lease[0] = 15
    worker.sweep(view, reaper, browser)
    assert reaper._idle == {}
    lease[0] = 0
    worker.sweep(view, reaper, browser)
    assert 'owned' in reaper._idle


def test_worker_never_starts_during_tests_or_demo(monkeypatch):
    import operator_tab_cleanup as worker
    monkeypatch.setattr(worker, '_thread', None)
    worker.start(types.SimpleNamespace(DEMO=False))
    assert worker._thread is None
    monkeypatch.delenv('OPERATOR_TESTING')
    worker.start(types.SimpleNamespace(DEMO=True))
    assert worker._thread is None


def test_cdp_probe_preserves_visible_and_edited_pages_in_real_chromium(tmp_path):
    """An isolated scratch Chrome, never the signed-in Operator browser.

    Headless Chrome has no real foreground window. Explicitly simulate hidden
    state ONLY in this blank test target to exercise the remaining DOM checks.
    """
    import subprocess
    import time
    import os
    pw = pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as playwright:
        executable = os.environ.get('OPERATOR_TEST_CHROMIUM') or playwright.chromium.executable_path
    if not os.path.exists(executable):
        pytest.skip(f'no Chromium at {executable}; set OPERATOR_TEST_CHROMIUM')
    # Chrome will not start a profile on the squad's tmpfs test temp
    # (/dev/shm, nosuid+nodev): no files, no port, no error. Use the bulk
    # scratch disk when the box has one, else pytest's temp.
    import shutil
    import tempfile
    scratch_root = os.environ.get('OPERATOR_TEST_CHROME_DIR') or (
        '/mnt/wsl-storage/scratch' if os.path.isdir('/mnt/wsl-storage/scratch') else str(tmp_path))
    profile = Path(tempfile.mkdtemp(prefix='op-scratch-chrome-', dir=scratch_root))
    # --disable-dev-shm-usage: a profile under /dev/shm (the squad's test
    # temp) starves Chrome's own shm and it never writes DevToolsActivePort
    proc = subprocess.Popen([executable, '--headless=new', '--no-sandbox',
        '--disable-gpu', '--disable-dev-shm-usage', '--no-first-run',
        '--disable-background-networking',
        '--remote-debugging-port=0', f'--user-data-dir={profile}', 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = profile / 'DevToolsActivePort'
        deadline = time.monotonic() + 30       # a loaded WSL box takes >10s
        while not port_file.exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(0.05)
        endpoint = 'http://127.0.0.1:' + port_file.read_text().splitlines()[0]
        # the port file lands before the HTTP endpoint answers; on a loaded
        # box the first /json call can outrun the client's own 5s budget
        import urllib.request
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(endpoint + '/json/version', timeout=2):
                    break
            except OSError:
                time.sleep(0.2)
        client = BT.TabCleanupClient(endpoint, timeout=15)   # scratch Chrome, loaded box
        owned = BT._new_target(endpoint)
        row = client.pages()[owned]
        assert not client.idle_page(row)
        client._rpc(row['webSocketDebuggerUrl'], 'Runtime.evaluate', {'expression':
            "Object.defineProperty(document, 'visibilityState', {value:'hidden'});"
            "document.hasFocus = () => false;"})
        assert client.idle_page(row)
        client._rpc(row['webSocketDebuggerUrl'], 'Runtime.evaluate', {'expression':
            "document.body.innerHTML = '<input value=original>';"
            "document.querySelector('input').value = 'edited';"})
        assert not client.idle_page(row)
        client._rpc(row['webSocketDebuggerUrl'], 'Runtime.evaluate', {'expression':
            "document.body.innerHTML = '';"})
        # every probe above held a debugger session on the page; Chrome reports
        # it attached until that session is torn down, and the reaper rightly
        # refuses an attached page. Wait for the detach before sweeping.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and client.pages()[owned].get('attached'):
            time.sleep(0.2)
        assert not client.pages()[owned].get('attached'), 'probe session never detached'
        path = tmp_path / 'owned.json'
        BT._write_registry({'done': [owned]}, path)
        reaper = BT.IdleTabReaper(path=path, idle_seconds=60)
        sweep(reaper, client, 0)
        result = sweep(reaper, client, 61)
        assert result['closed'] == 1
        assert owned not in client.pages()
        assert len(client.pages()) == 1  # the unowned original page survives
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(profile, ignore_errors=True)


def test_a_close_that_chrome_confirms_late_still_counts(setup):
    """/json/close is asynchronous: under load the target is still listed on
    the very next /json/list. The reaper used to read that as "not closed",
    kept the registry entry for a dead tab and reported closed=0
    (2026-09-22, seen on Chrome 153 with the box at load 13)."""
    path, browser, reaper = setup

    class Lagging(Browser):
        def __init__(self):
            super().__init__()
            self.pending = None

        def close(self, target):
            self.closed.append(target)
            self.pending = target             # gone on the NEXT listing, not this one

        def pages(self):
            rows = super().pages()
            if self.pending is not None:
                self.pending = None
                return rows                    # one stale listing
            for tid in list(rows):
                if tid in self.closed:
                    rows.pop(tid)
                    self.rows.pop(tid, None)
            return rows
    browser = Lagging()
    sweep(reaper, browser, 0)
    result = sweep(reaper, browser, 60)
    assert browser.closed == ["owned"]
    assert result["closed"] == 1
    assert BT._read_registry(path) == {}
