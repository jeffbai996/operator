"""Small interaction checks for the 1.2.5 result-card polish."""
import pytest

from cockpit_support import (  # noqa: F401
    browser, _fresh_session_store, _Harness, _DEAD_CDP,
)


@pytest.fixture(scope='module')
def harness():
    # The shared harness restores collection-time environment after its module.
    # Re-establish isolation here so either module can run first.
    with pytest.MonkeyPatch.context() as env:
        env.setenv('OPERATOR_DEMO_CDP', _DEAD_CDP)
        env.delenv('OPERATOR_DEMO', raising=False)
        env.setenv('OPERATOR_CHROME_LAUNCHER', '/nonexistent/operator-harness-launcher')
        server = _Harness()
        try:
            yield server
        finally:
            server.stop()


@pytest.mark.parametrize('copy_fails', [False, True])
def test_order_copy_reports_visible_outcome(browser, harness, copy_fails):
    import operator_workspace as ws

    run = ws.start_run('legacy', 'Check an order')
    evidence = ws.observe(run['run_id'], 'browser_snapshot', 'Order confirmation',
                          ['https://www.amazon.ca/gp/your-account/order-history'])
    result = ws.publish_result(run['run_id'], run['credential'], {
        'title': 'Order confirmed', 'status': 'confirmed', 'summary': 'One item.',
        'confirmation': 'Amazon.ca order 701-1111111-2222222',
        'evidence_ids': [evidence['id']],
    })
    ws.finish_run(run['run_id'], 'done')
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    ctx.add_init_script("""Object.defineProperty(navigator, 'clipboard', {value: {
        writeText: async text => {
            window.copiedOrder = text;
            if (%s) throw new Error('Clipboard denied');
        }
    }});""" % str(copy_fails).lower())
    page = ctx.new_page()
    harness.mode = 'live'
    page.route('**/operator/workspace?*', lambda route: route.fulfill(
        json=dict(ok=True, **ws.snapshot('legacy'))))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.evaluate("document.getElementById('op-lp').style.display='none'; document.querySelector('.op').dataset.mode='auto'")
        card = page.locator(f'[data-workbench-card="{result["id"]}"]')
        copy = card.get_by_role('button', name='Copy order number 701-1111111-2222222')
        copy.click()
        assert page.evaluate('window.copiedOrder') == '701-1111111-2222222'
        feedback = card.get_by_role('status')
        assert feedback.is_visible()
        assert feedback.inner_text() == ('Copy failed' if copy_fails else 'Copied')
        assert copy.inner_text() == '701-1111111-2222222'
    finally:
        ctx.close()
