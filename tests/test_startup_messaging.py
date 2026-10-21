"""A slow first connection is distinct from losing an established feed."""
import pytest
from cockpit_support import browser, _fresh_session_store, _Harness, _DEAD_CDP, OV  # noqa: F401
from playwright.sync_api import Page, Route


@pytest.fixture(scope='module')
def harness():
    with pytest.MonkeyPatch.context() as env:
        env.setenv('OPERATOR_DEMO_CDP', _DEAD_CDP)
        env.delenv('OPERATOR_DEMO', raising=False)
        env.setenv('OPERATOR_CHROME_LAUNCHER', '/nonexistent/operator-harness-launcher')
        server = _Harness()
        try:
            yield server
        finally:
            server.stop()


def test_slow_start_stays_connecting_but_real_failure_is_visible(browser, harness):
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    state = {'status': 'connecting', 'detail': 'starting browser…',
             'has_frame': False, 'browser_up': False, 'surface': 'browser'}
    page = ctx.new_page()
    page.clock.install()
    status_polls = []

    def status_response(route: Route) -> None:
        status_polls.append(state['status'])
        route.fulfill(json=state)

    page.route('**/operator/status', status_response)
    page.route('**/operator/frame?*', lambda r: r.fulfill(
        body=OV._PLACEHOLDER_JPEG, content_type='image/jpeg',
        headers={'X-Operator-Frame': 'placeholder'}))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        # Exercise the full startup grace period without sleeping in real time.
        page.clock.run_for(7500)
        assert len(status_polls) >= 5
        assert not page.locator('.op').evaluate("e => e.classList.contains('op-signal-lost')")
        assert page.locator('.op').get_attribute('data-state') == 'connecting'
        state.update(status='error', detail='browser could not start')
        page.wait_for_function("document.querySelector('.op').classList.contains('op-signal-lost')")
        assert 'Unable to connect' in page.locator('.op').inner_text()
    finally:
        ctx.close()


def _observe_status_and_agent_polls(page: Page) -> None:
    # Both pollers can overwrite the headline. Observe completed responses,
    # rather than assume that a fixed sleep included the expected cycles.
    with page.expect_response('**/operator/status'):
        for _ in range(2):
            with page.expect_response('**/operator/agent?*'):
                pass


def test_task_headline_survives_browser_polling_and_approval_changes(browser, harness, tmp_path):
    import operator_workspace as ws
    from playwright.sync_api import expect

    run = ws.start_run('legacy', 'Review a booking')
    snapshot = {'state': 'running', 'run_id': run['run_id'], 'started_ts': 2000000000,
                'alive': True, 'messages': []}
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page()
    page.route('**/operator/agent?*', lambda r: r.fulfill(json=snapshot))
    page.route('**/operator/status', lambda r: r.fulfill(json={
        'status': 'connecting', 'detail': 'starting browser…', 'has_frame': False,
        'browser_up': False, 'surface': 'browser'}))
    page.route('**/operator/workspace?*', lambda r: r.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        headline = page.locator('#op-action-txt')
        expect(headline).to_have_text('In progress')
        approval = ws.request_approval(run['run_id'], run['credential'], {
            'kind': 'purchase', 'destination': 'example.com', 'description': 'Book one room'})
        expect(headline).to_have_text('Awaiting approval', timeout=10000)
        _observe_status_and_agent_polls(page)
        expect(headline).to_have_text('Awaiting approval')
        ws.decide_approval('legacy', approval['id'], approval['fingerprint'], True)
        expect(headline).to_have_text('In progress', timeout=10000)
        snapshot.update(state='done', alive=False, final='Booking reviewed.')
        expect(headline).to_have_text('Finished', timeout=10000)
        _observe_status_and_agent_polls(page)
        expect(headline).to_have_text('Finished')
        page.add_style_tag(content=":root{--fg:#e7ecf3;--fg-2:#b8c0cc;--muted:#7e8a9a;--border:#292b30;--border-2:#30343b;--bg:#000;--bg-2:#101216} body{background:#000;color:var(--fg)}")
        page.evaluate("""() => {
          document.getElementById('op-lp').hidden = true;
          document.getElementById('op-thread-observer').hidden = false;
          document.getElementById('op-thread-observer-text').textContent = 'Another device has control';
          document.getElementById('op-model').innerHTML = '<option>GPT-6.1 Sol</option>';
          document.getElementById('op-effort').innerHTML = '<option>medium</option>';
        }""")
        page.locator('.op-rail').screenshot(path=str(tmp_path / 'operator-status-mobile.png'))
    finally:
        ctx.close()
