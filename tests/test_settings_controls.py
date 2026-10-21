"""Settings popover controls share one visual language."""
import pytest

from cockpit_support import (  # noqa: F401
    browser, _Harness, _DEAD_CDP,
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


def test_settings_rows_use_matching_switches_and_buttons(browser, harness):
    ctx = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = ctx.new_page()
    harness.mode = 'live'
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function('!!window.OperatorWorkbench')
        page.evaluate("document.getElementById('op-lp').style.display='none'")
        page.locator('#op-ham-btn').click()
        page.locator('#op-ham-settings').click()
        page.wait_for_selector('#op-settings:not([hidden])')
        diagnostics = page.locator('#op-diagnostics-toggle')
        if not diagnostics.is_checked():
            diagnostics.click()
        page.wait_for_selector('#op-health-open:not([hidden])')

        metrics = page.evaluate("""() => {
          const box = id => document.getElementById(id).getBoundingClientRect();
          const css = id => getComputedStyle(document.getElementById(id));
          const hint = document.getElementById('op-set-hint');
          hint.style.transition = 'none';   // read settled colours, not a transition's first frame
          hint.checked = false;
          const offTrack = getComputedStyle(hint).backgroundColor;
          hint.checked = true;
          const onTrack = getComputedStyle(hint).backgroundColor;
          return {
            diagnostics: [box('op-diagnostics-toggle').width, box('op-diagnostics-toggle').height],
            hint: [box('op-set-hint').width, box('op-set-hint').height],
            diagnosticsAppearance: css('op-diagnostics-toggle').appearance,
            healthHeight: box('op-health-open').height,
            resetHeight: box('op-set-home-reset').height,
            healthFont: css('op-health-open').fontSize,
            resetFont: css('op-set-home-reset').fontSize,
            switchRight: box('op-diagnostics-toggle').right,
            hintRight: box('op-set-hint').right,
            offTrack, onTrack,
          };
        }""")
        assert metrics['diagnosticsAppearance'] == 'none'
        assert metrics['diagnostics'] == metrics['hint']
        assert abs(metrics['switchRight'] - metrics['hintRight']) <= 1
        assert metrics['healthFont'] == metrics['resetFont']
        assert metrics['healthHeight'] <= metrics['resetHeight'] + 1
        assert metrics['onTrack'] != metrics['offTrack']
    finally:
        harness.mode = 'real'
        ctx.close()


def test_agy_claude_models_keep_effort_in_the_disabled_picker(browser, harness):
    context = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = context.new_page()
    page.route('**/operator/models?*', lambda route: route.fulfill(json={
        'default_model': 'claude-sonnet-5-5-medium',
        'models': [
            {'value': 'claude-sonnet-5-5-medium', 'label': 'Sonnet 5.5 (Medium)'},
            {'value': 'claude-opus-5-5-medium', 'label': 'Opus 5.5 (Medium)'},
            {'value': 'gemini-4-argon', 'label': '4 Argon'},
        ],
    }))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function('!!window.OperatorWorkbench')
        page.evaluate("window._opLoadModels('gemma')")
        for model, label in [('claude-sonnet-5-5-medium', 'Sonnet 5.5'),
                             ('claude-opus-5-5-medium', 'Opus 5.5')]:
            page.select_option('#op-model', model)
            assert page.locator('#op-model option:checked').inner_text() == label
            effort = page.locator('#op-effort')
            assert effort.is_disabled()
            assert not effort.evaluate('(element) => element.hidden')
            assert effort.input_value() == 'medium'
        page.select_option('#op-model', 'gemini-4-argon')
        assert page.locator('#op-effort').is_enabled()
        assert page.locator('#op-effort option').all_text_contents() == ['low', 'medium', 'high']
    finally:
        context.close()


def test_native_opus_alias_keeps_the_effort_selector(browser, harness):
    context = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = context.new_page()
    page.route('**/operator/models?*', lambda route: route.fulfill(json={
        'default_model': 'opus',
        'models': [{'value': 'opus', 'label': 'Opus 5.5'},
                   {'value': 'claude-sonnet-5-5', 'label': 'Sonnet 5.5'}],
    }))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function('!!window.OperatorWorkbench')
        page.evaluate("window._opLoadModels('claude-a')")
        page.wait_for_function("document.querySelector('#op-model').value === 'opus'")
        effort = page.locator('#op-effort')
        assert not effort.evaluate('(element) => element.hidden')
        assert effort.is_enabled()
        assert effort.locator('option').all_text_contents() == ['low', 'medium', 'high', 'xhigh', 'max']
        effort.select_option('high')
        page.select_option('#op-model', 'claude-sonnet-5-5')
        page.select_option('#op-model', 'opus')
        assert effort.input_value() == 'high'
    finally:
        context.close()
