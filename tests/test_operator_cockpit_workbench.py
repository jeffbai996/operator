"""Workbench, approval, result and usage contracts for the real operator_view page.

Run through scripts/cc_test.py with this explicit file. Shared fixtures and
isolation live in cockpit_support. Shared dependencies are operator_view,
operator_workspace, operator.js, operator_feed.js, workbench.js, operator.css
and workbench.css; their boot and state contracts span these groups.
"""

from cockpit_support import (  # noqa: F401
    OS_MOD,
    OV,
    Path,
    _SEEDED_SESSION,
    _fresh_session_store,
    _restore_harness_env,
    _seed_once_script,
    _token_host_styles,
    browser,
    harness,
    json,
    os,
    page,
    pytest,
)


def test_agent_action_label_is_rendered_as_text_in_status(browser, harness):
    """Untrusted action labels stay text in the full status row."""
    harness.agent_mode = "running"
    hostile_verb = (
        '<img id="agent-action-xss" src="missing" '
        'onerror="document.body.id=\'agent-action-xss-fired\'">'
    )
    harness.agent_messages = [{
        "ts": 2_000_000_001,
        "role": "action",
        "text": hostile_verb,
        "detail": "",
    }]
    ctx = browser.new_context()
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op-action-sub').textContent.trim() === " + json.dumps(hostile_verb),
            timeout=8000,
            polling=50,
        )

        sub = pg.locator("#op-action-sub")
        assert pg.locator("#agent-action-xss").count() == 0
        assert pg.locator("body").get_attribute("id") != "agent-action-xss-fired"
        assert sub.text_content() == hostile_verb
        assert pg.locator('#op-min-emoji').text_content() == '⚙️'

        # Repeated identical status polls must keep the old no-strobe contract.
        pg.evaluate("""() => {
          window.__sublineClassMutations = 0;
          new MutationObserver(ms => { window.__sublineClassMutations += ms.length; })
            .observe(document.getElementById('op-action-sub'), {
              attributes: true, attributeFilter: ['class']
            });
        }""")
        pg.wait_for_timeout(1000)
        assert pg.evaluate("window.__sublineClassMutations") == 0
    finally:
        ctx.close()
        harness.agent_mode = None
        harness.agent_messages = []


def test_running_status_shows_full_action_and_separate_token_meter(browser, harness):
    harness.agent_mode = 'running'
    harness.agent_messages = [{'ts': 2_000_000_002, 'role': 'action',
                               'text': 'Verifying Canadian OTC fluoride strengths', 'detail': ''}]
    harness.agent_in_tokens = 48_127
    harness.agent_out_tokens = 913
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page(); errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function("document.getElementById('op-action-sub').textContent === 'Verifying Canadian OTC fluoride strengths'")
        meter = page.locator('#op-token-meter')
        page.wait_for_function("document.querySelector('.op-step-count #op-token-meter')?.hidden === false")
        assert meter.is_visible()
        assert ''.join(meter.text_content().split()) == '↑48.1k↓913'
        assert 'Input: 48,127 tokens' in meter.get_attribute('title')
        assert 'Output: 913 tokens' in meter.get_attribute('title')
        geom = page.evaluate('''() => {
          const card=document.getElementById('op-action').getBoundingClientRect();
          const meter=document.getElementById('op-token-meter').getBoundingClientRect();
          return {cardRight:card.right, meterRight:meter.right, meterLeft:meter.left}; }''')
        assert geom['meterLeft'] >= 0 and geom['meterRight'] <= geom['cardRight']
        output = os.environ.get('OPERATOR_QA_OUTPUT')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            page.locator('#op-action').screenshot(path=str(Path(output) / 'status-card.png'))
        assert errors == []
    finally:
        ctx.close()


def test_midrun_message_uses_owned_steering_without_client_restart(browser, harness):
    """1.2: native/boundary steering is server-owned; the browser must never
    race a replacement against the still-running process. Stop stays separate."""
    harness.agent_mode = "running"
    harness.say_posts.clear()
    harness.stop_posts.clear()
    harness.dispatch_posts.clear()
    ctx = browser.new_context()
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.bring_to_front()
        pg.wait_for_function("typeof window._opThreadHeartbeat === 'function'",
                             polling=100)
        pg.evaluate("window._opThreadHeartbeat(true)")
        pg.wait_for_function(
            "document.getElementById('op').dataset.threadControl === 'controller'",
            timeout=5000, polling=100)
        # the agent poll marks the run in-flight → the send button flips to ■
        pg.wait_for_function(
            "document.getElementById('op-send').classList.contains('stopping')",
            timeout=8000, polling=100)
        pg.fill("#op-input", "switch to the CAD listing")
        pg.press("#op-input", "Enter")
        pg.wait_for_timeout(1500)
        assert harness.stop_posts == [], 'steering must not use the Stop endpoint'
        assert harness.dispatch_posts == [], 'the client must not launch a replacement'
        assert harness.say_posts == ['switch to the CAD listing']
        assert pg.locator("#op-log .op-msg.user").count() == 1
        assert errors == [], f"JS errors during steer: {errors}"
    finally:
        harness.agent_mode = None
        ctx.close()


def test_workbench_jobs_files_and_diagnostics_are_compact(browser, harness):
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Find a tasting')
    ws.update_job(run['run_id'], run['credential'], {'constraints': ['Two adults'],
        'checkpoints': [{'step': 'Compare opening times', 'status': 'inProgress'}]})
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        # This fixture's minimal host template has no theme tokens. Supply the
        # actual host's midnight palette for visual QA (not geometry overrides).
        page.add_style_tag(content=':root {--fg:#e7ecf3;--muted:#7e8a9a;--border:#292b30;--border-2:#30343b} body {color:var(--fg);background:#000}')
        page.wait_for_function('!!window.OperatorWorkbench')
        page.wait_for_function("document.getElementById('op-files-open').hidden === false")
        assert page.locator('#op-health-open').is_hidden()
        # Project a fixture state through the real endpoint and module; no agent dispatch.
        page.route('**/operator/workspace?*', lambda route: route.fulfill(json= dict(ok=True, **ws.snapshot('legacy'))))
        page.evaluate("window.OperatorWorkbench.update({alive:true, run_id:'fixture'}, window.OperatorWorkbenchBridge.context().conversation_id || 'legacy')")
        page.wait_for_timeout(2000)
        page.evaluate('window.OperatorWorkbench.toggleJob()')
        assert 'Two adults' in page.locator('#op-job-panel').inner_text()
        page.locator('#op-files-open').dispatch_event('click')
        with page.expect_file_chooser() as picker:
            page.get_by_role('button', name='Add files', exact=True).click()
        assert picker.value.is_multiple()
        assert page.locator('#op-files-dialog').is_visible()
        assert errors == []
    finally:
        ws.finish_run(run['run_id'], 'done')
        ctx.close()


@pytest.mark.parametrize('width,height', [(390, 844), (1440, 900)])
def test_workbench_results_approvals_and_recipe_choices(browser, harness, width, height):
    import operator_workspace as ws
    from pathlib import Path
    run = ws.start_run('legacy', 'Find a tasting')
    ws.update_job(run['run_id'], run['credential'], {'constraints': ['Two adults, Tuesday'],
        'checkpoints': [{'step': 'Compare tasting times', 'status': 'completed'},
                        {'step': 'Prepare the booking', 'status': 'inProgress'}]})
    ev = ws.observe(run['run_id'], 'browser_snapshot', 'Fixture availability', ['https://example.com/tastings'])
    ws.publish_result(run['run_id'], run['credential'], {'title': 'Tuesday tasting', 'status': 'prepared',
        'summary': 'Two places at 10:00. Ready for your approval.', 'evidence_ids': [ev['id']]})
    ws.request_approval(run['run_id'], run['credential'], {'kind': 'booking', 'destination': 'example.com',
        'description': 'Reserve two places for Tuesday at 10:00', 'amount': 60, 'currency': 'CAD'})
    ctx = browser.new_context(viewport={'width': width, 'height': height})
    harness.mode = 'live'
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
        page.route('**/operator/steer', lambda route: route.fulfill(json={'ok': True}))
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.add_style_tag(content=':root {--fg:#e7ecf3;--muted:#7e8a9a;--border:#292b30;--border-2:#30343b} body {color:var(--fg);background:#000}')
        page.wait_for_function('!!window.OperatorWorkbench')
        # Workspace hydration can dismiss the welcome screen between a
        # visibility check and click. This is setup, not the control under test.
        page.locator('#op-lp-x').dispatch_event('click')
        page.wait_for_selector('.op-wb-approval', state='visible')
        prepared = page.locator('.op-wb-card:has(.op-wb-tag-prepared)')
        assert prepared.locator('.op-wb-tag').inner_text() == 'Ready for approval'
        assert prepared.locator('.op-wb-tag-icon svg circle').count() == 1
        page.evaluate('window.OperatorWorkbench.toggleJob()')
        assert page.locator('.op-wb-approval .op-wb-amount-value').inner_text() == '$60.00'
        assert page.locator('.op-wb-approval .op-wb-amount-code').inner_text() == 'CAD'
        box = page.locator('.op-wb-approval').bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
        page.evaluate("window.OperatorWorkbench.recipeFill({authorization:{mode:'bounded', actions:['booking','message'], destinations:['example.com'],max_amount:60,currency:'CAD'}})")
        assert page.evaluate('window.OperatorWorkbench.recipeFields().authorization.actions') == ['booking', 'message']
        output = os.environ.get('OPERATOR_QA_OUTPUT')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(output) / f'workbench-{width}.png'), full_page=True)
            page.locator('.op-wb-approval').scroll_into_view_if_needed()
            page.screenshot(path=str(Path(output) / f'approval-{width}.png'), full_page=True)
        assert not errors
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()


def test_workbench_state_labels_share_size_and_closed_approvals_are_distinct(browser, harness):
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Review the options')
    first = ws.request_approval(run['run_id'], run['credential'], {
        'kind': 'purchase', 'destination': 'example.com', 'description': 'Option one'})
    second = ws.request_approval(run['run_id'], run['credential'], {
        'kind': 'purchase', 'destination': 'example.com', 'description': 'Option two'})
    ws.decide_approval('legacy', second['id'], second['fingerprint'], False)
    ws.request_approval(run['run_id'], run['credential'], {
        'kind': 'purchase', 'destination': 'example.com', 'description': 'Option three'})
    ev = ws.observe(run['run_id'], 'browser_snapshot', 'Observed confirmation', ['https://example.com/order'])
    ws.publish_result(run['run_id'], run['credential'], {
        'title': 'Option three ready', 'status': 'prepared', 'summary': 'Ready to review.'})
    ws.publish_result(run['run_id'], run['credential'], {
        'title': 'Earlier order', 'status': 'confirmed', 'summary': 'Placed.',
        'confirmation': 'Order 123', 'evidence_ids': [ev['id']]})
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page(); harness.mode = 'live'
    page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.add_style_tag(content=':root{--fg:#e7ecf3;--fg-2:#b3bac5;--muted:#79828d;--bg:#000;--bg-3:#12141a;--border:#292b30} body{color:var(--fg);background:#000}')
        page.evaluate("document.getElementById('op-lp').style.display='none'; document.querySelector('.op').dataset.mode='auto'")
        replaced = page.locator(f'[data-workbench-card="{first["id"]}"]')
        declined = page.locator(f'[data-workbench-card="{second["id"]}"]')
        replaced.wait_for(state='visible')
        assert replaced.locator('.op-wb-approval-heading').inner_text() == 'Replaced'
        assert declined.locator('.op-wb-approval-heading').inner_text() == 'Declined'
        assert replaced.locator('.op-wb-approval-icon svg circle').count() == 0
        assert replaced.locator('.op-wb-approval-icon svg path').get_attribute('d').startswith('M4.5 7h10')
        assert declined.locator('.op-wb-approval-icon svg circle').count() == 1
        assert replaced.evaluate("el => getComputedStyle(el).borderColor") != declined.evaluate("el => getComputedStyle(el).borderColor")
        assert replaced.locator('.op-wb-approval-heading').evaluate("el => getComputedStyle(el).color") != declined.locator('.op-wb-approval-heading').evaluate("el => getComputedStyle(el).color")
        prepared = page.locator('.op-wb-tag-prepared')
        confirmed = page.locator('.op-wb-tag-confirmed')
        assert prepared.inner_text() == 'Ready for approval'
        assert confirmed.inner_text() == 'Confirmed'
        labels = [replaced.locator('.op-wb-approval-heading'), declined.locator('.op-wb-approval-heading'), prepared, confirmed]
        sizes = [label.evaluate('el => getComputedStyle(el).fontSize') for label in labels]
        assert sizes[0] == sizes[1]
        assert sizes[2] == sizes[3]
        output = os.environ.get('OPERATOR_QA_OUTPUT')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            for name, card in [('replaced', replaced), ('declined', declined),
                               ('prepared', prepared.locator('xpath=ancestor::article')),
                               ('confirmed', confirmed.locator('xpath=ancestor::article'))]:
                card.screenshot(path=str(Path(output) / f'state-{name}.png'))
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()


@pytest.mark.parametrize("favicon_loads", [True, False], ids=["loaded", "fallback"])
def test_approval_heading_and_amount_fit_wide_rail(browser, harness, favicon_loads):
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Review the cart')
    ws.request_approval(run['run_id'], run['credential'], {
        'kind': 'purchase', 'destination': 'best buy canada',
        'description': 'GMKtec EVO-X2 AI Mini PC Ryzen AI Max+ 395',
        'amount': 5099.99, 'currency': 'CAD'})
    ctx = browser.new_context(viewport={'width': 1440, 'height': 900})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    harness.mode = 'live'
    page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    favicon_url = "https://www.google.com/s2/favicons?domain=bestbuy.ca&sz=32"
    if favicon_loads:
        page.route(favicon_url, lambda route: route.fulfill(
            content_type="image/svg+xml",
            body='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                 '<circle cx="8" cy="8" r="7"/></svg>'))
    else:
        page.route(favicon_url, lambda route: route.abort())
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.add_style_tag(content=':root {--fg:#e7ecf3;--muted:#7e8a9a;--border:#292b30;--border-2:#30343b} body {color:var(--fg);background:#000}')
        page.wait_for_function("document.getElementById('op-lp-x')._wired === true")
        page.locator('#op-lp-x').dispatch_event('click')
        page.evaluate("""() => {
          const op = document.getElementById('op');
          op.style.setProperty('--rail-w', '670px');
          op.style.setProperty('--chat-scale', '1.85');
        }""")
        heading = page.locator('.op-wb-approval-heading')
        heading.wait_for(state='visible')
        merchant = page.locator('.op-wb-approval h3.op-wb-merchant')
        assert merchant.inner_text() == 'Best Buy Canada'
        if favicon_loads:
            favicon = merchant.locator('img')
            page.wait_for_function(
                "img => img.complete && img.naturalWidth > 0", arg=favicon.element_handle())
            assert favicon.count() == 1
            assert favicon.get_attribute('src') == favicon_url
        else:
            page.wait_for_function(
                "document.querySelector('.op-wb-approval h3.op-wb-merchant img') === null")
            assert merchant.locator('img').count() == 0
        assert merchant.inner_text() == 'Best Buy Canada'
        page.wait_for_function("() => { const h=document.querySelector('.op-wb-approval-heading'); return h.lastElementChild.getBoundingClientRect().right <= h.getBoundingClientRect().right + 1; }")
        metrics = heading.evaluate("""el => {
          const label = el.lastElementChild;
          const amount = el.closest('.op-wb-approval').querySelector('.op-wb-amount-value');
          const merchant = el.closest('.op-wb-approval').querySelector('h3.op-wb-merchant');
          const labelRange = document.createRange(); labelRange.selectNodeContents(label);
          return {lines:labelRange.getClientRects().length,
            labelRight:label.getBoundingClientRect().right,
            headingRight:el.getBoundingClientRect().right,
            headingSize:parseFloat(getComputedStyle(el).fontSize),
            merchantSize:parseFloat(getComputedStyle(merchant).fontSize),
            amountSize:parseFloat(getComputedStyle(amount).fontSize),
            amountFont:getComputedStyle(amount).fontFamily,
            numerals:getComputedStyle(amount).fontVariantNumeric};
        }""")
        assert metrics['lines'] == 1 and metrics['labelRight'] <= metrics['headingRight'] + 1
        assert metrics['amountSize'] > max(metrics['headingSize'], metrics['merchantSize'])
        assert 'DM Sans' in metrics['amountFont'] and metrics['numerals'] == 'proportional-nums'
        decimal = page.locator('.op-wb-decimal')
        assert decimal.inner_text() == '.'
        assert decimal.evaluate('el => getComputedStyle(el).display') == 'inline'
        assert errors == [], f'JS errors: {errors}'
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()


def test_expired_approval_card_is_visible_but_not_actionable(browser, harness):
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Review the cart')
    approval = ws.request_approval(run['run_id'], run['credential'], {
        'kind': 'purchase', 'destination': 'best buy canada',
        'description': 'One item', 'amount': 37.53, 'currency': 'CAD'})
    with ws.database() as db:
        job = ws._job(db.execute('SELECT * FROM jobs WHERE id=?', (run['job']['id'],)).fetchone())
        job['approvals'][0]['created'] -= ws.APPROVAL_TIMEOUT_SECONDS + 1
        ws._save(db, job)
    ctx = browser.new_context(viewport={'width': 1000, 'height': 800})
    page = ctx.new_page(); harness.mode = 'live'
    page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        card = page.locator(f'[data-workbench-card="{approval["id"]}"]')
        card.wait_for(state='visible')
        assert 'Approval expired' in card.inner_text()
        assert 'Best Buy Canada' in card.inner_text()
        assert card.locator('button').count() == 0
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()


def test_approval_change_request_keeps_composer_draft_and_requires_new_approval(browser, harness):
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Review booking')
    approval = ws.request_approval(run['run_id'], run['credential'], {
        'kind': 'booking', 'destination': 'example.com', 'description': 'One room'})
    ctx = browser.new_context(viewport={'width': 1000, 'height': 800})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page(); harness.mode = 'live'; dispatches = []
    page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    page.route('**/operator/dispatch', lambda route: (
        dispatches.append(route.request.post_data_json), route.fulfill(json={'ok': True})))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.evaluate("document.getElementById('op-lp-x')?.click()")
        card = page.locator(f'[data-workbench-card="{approval["id"]}"]')
        card.wait_for(state='visible')
        page.locator('#op-input').fill('Unsent draft')
        card.get_by_role('button', name='Request changes').click()
        card.get_by_role('textbox', name='What should Operator change?').fill('Use Visa ending 9860')
        card.get_by_role('button', name='Send changes').click()
        page.wait_for_function("!document.querySelector('.op-wb-change-editor:not([hidden])')")
        assert ws.snapshot('legacy')['job']['approvals'][0]['status'] == 'changes_requested'
        assert page.locator('#op-input').input_value() == 'Unsent draft'
        assert 'Use Visa ending 9860' in ' '.join(page.locator('.op-msg.user').all_inner_texts())
        assert any('Request approval again' in data['task'] for data in dispatches)
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()


def test_approval_review_can_back_out_and_resumes_without_a_visible_internal_prompt(browser, harness):
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Review the booking')
    ws.request_approval(run['run_id'], run['credential'], {
        'kind': 'booking', 'destination': 'example.com',
        'description': 'Reserve one room', 'amount': 60, 'currency': 'CAD'})
    ctx = browser.new_context(viewport={'width': 1000, 'height': 800})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page(); dispatches = []
    harness.mode = 'live'
    page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    page.route('**/operator/dispatch', lambda route: (
        dispatches.append(route.request.post_data_json), route.fulfill(json={'ok': True})))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.add_style_tag(content=':root {--fg:#e7ecf3;--muted:#7e8a9a;--border:#292b30;--border-2:#30343b} body {color:var(--fg);background:#000}')
        page.wait_for_selector('.op-wb-approval')
        card = page.locator('.op-wb-approval')
        assert 'Operator needs your permission' in card.inner_text()
        assert card.locator('.op-wb-approval-icon svg path').count() == 2
        page.evaluate("""() => {
          const tail = document.createElement('div');
          tail.id = 'approval-later-trace'; tail.style.height = '800px';
          document.querySelector('.op-wb-approval').after(tail);
        }""")
        card.scroll_into_view_if_needed()
        page.evaluate("""() => { window.__approvalCard = document.querySelector('.op-wb-approval');
          window.__approvalTop = window.__approvalCard.getBoundingClientRect().top; }""")
        page.wait_for_function("!document.querySelector('.op-wb-decline').disabled")
        card.get_by_role('button', name='Decline').hover()
        page.wait_for_function("""() => {
          const button = document.querySelector('.op-wb-decline');
          return button && button.matches(':hover') &&
            getComputedStyle(button).borderColor === 'rgb(224, 100, 100)' &&
            getComputedStyle(button).boxShadow !== 'none';
        }""")
        page.evaluate("""() => { window.__approvalButtonRects =
          [...document.querySelectorAll('.op-wb-approval-actions > button')].map(b => {
            const r = b.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width};
          }); }""")
        card.get_by_role('button', name='Approve', exact=True).click()
        assert page.evaluate("""() => [...document.querySelectorAll('.op-wb-approval-actions > button')]
          .every((b,i) => { const r=b.getBoundingClientRect(), before=window.__approvalButtonRects[i];
            return Math.abs(r.x-before.x)<1 && Math.abs(r.y-before.y)<1 &&
              Math.abs(r.width-before.width)<1; })""")
        assert card.locator('.op-wb-decline').evaluate("el => getComputedStyle(el).visibility") == 'hidden'
        assert page.evaluate("""() => Math.abs(
          document.querySelector('.op-wb-approval').getBoundingClientRect().top - window.__approvalTop) < 2""")
        assert page.evaluate("""() => document.querySelector('.op-wb-approval').nextElementSibling?.id
          === 'approval-later-trace'""")
        assert card.get_by_role('button', name='Confirm?', exact=True).is_visible()
        assert card.locator('.op-wb-confirm-prompt').count() == 0
        card.get_by_role('button', name='Back').click()
        assert card.get_by_role('button', name='Approve', exact=True).is_visible()
        assert ws.snapshot('legacy')['job']['approvals'][0]['status'] == 'pending'

        page.evaluate("""() => { window.__approvalMorphSeen = false;
          new MutationObserver(() => {
            if (document.querySelector('.op-wb-approval.is-approved.op-wb-approval-morph'))
              window.__approvalMorphSeen = true;
          }).observe(document.getElementById('op-log'), {childList:true,subtree:true,attributes:true,attributeFilter:['class']});
        }""")
        card.get_by_role('button', name='Approve', exact=True).click()
        card.get_by_role('button', name='Confirm?', exact=True).click()
        page.wait_for_selector('.op-wb-approval.is-approved')
        assert page.evaluate('window.__approvalMorphSeen')
        assert 'Approved' in card.inner_text()
        assert card.locator('.op-wb-approval-icon svg circle').count() == 1
        assert card.locator('.op-wb-approved-continue').is_disabled()
        assert 'Reserve one room' not in card.inner_text()
        assert card.locator('.op-wb-amount-value').inner_text() == '$60.00'
        assert card.locator('.op-wb-amount-code').inner_text() == 'CAD'
        assert page.evaluate('window._opLastDispatch') is None
        assert all('Continue with the approved action' not in text
                   for text in page.locator('.op-msg.user').all_inner_texts())
        assert len(dispatches) == 1
        assert 'Continue with the approved action' in dispatches[0]['task']
        output = os.environ.get('OPERATOR_QA_OUTPUT')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            card.screenshot(path=str(Path(output) / 'approval-approved.png'))
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()


def test_approval_amount_uses_currency_digits_and_separate_code(
        browser, harness):
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Review an amount')
    ctx = browser.new_context(viewport={'width': 1000, 'height': 800})
    page = ctx.new_page(); harness.mode = 'live'
    page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    try:
        for i, (currency, amount, expected) in enumerate([
            ('CAD', 60, '$60.00'), ('USD', 1234.5, '$1,234.50'),
            ('EUR', 60, '€60.00'), ('JPY', 6000, '¥6,000'),
            ('KWD', 3.125, '3.125'), ('INR', 1400.5, '₹1,400.50'),
        ]):
            approval = ws.request_approval(run['run_id'], run['credential'], {
                'kind': 'booking', 'destination': 'example.com',
                'description': 'Reserve one room', 'amount': amount, 'currency': currency})
            assert approval['status'] == 'pending'
            if i == 0:
                page.goto(harness.base + '/operator', wait_until='domcontentloaded')
            else:
                # Exercise the normal foreground refresh, without a polling sleep.
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            card = page.locator(f'[data-workbench-card="{approval["id"]}"]')
            card.wait_for(state='attached')
            assert card.locator('.op-wb-amount-value').inner_text() == expected, currency
            assert card.locator('.op-wb-amount-code').inner_text() == currency
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()


def test_result_markdown_and_delayed_transcript_stay_in_turn(browser, harness):
    import operator_workspace as ws
    initial = ws.start_run('legacy', 'Find hotels')
    ws.finish_run(initial['run_id'], 'done')
    run = ws.start_run('legacy', 'Compare rates')
    ev = ws.observe(run['run_id'], 'browser_snapshot', 'Rates', ['https://example.com'])
    ws.publish_result(run['run_id'], run['credential'], {'title': 'Rates', 'status': 'found',
        'summary': '**Verified**\n\n| Date | Price |\n| --- | --- |\n| Friday | $288 |\n\n```python\nprint(288)\n```\n\n<img src=x onerror=alert(1)>', 'evidence_ids': [ev['id']]})
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    page = ctx.new_page()
    page.route('**/operator/workspace?*', lambda r: r.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_selector('[data-workbench-job]', state='attached')
        page.evaluate('''() => {
          const log = document.getElementById('op-log');
          const user = document.createElement('div'); user.className='op-msg user';
          user.innerHTML='<span class="bubble">Compare rates</span>'; log.prepend(user);
          const earlier = document.createElement('div'); earlier.className='op-msg user';
          earlier.innerHTML='<span class="bubble">Find hotels</span>'; log.prepend(earlier);
          const late = document.createElement('div'); late.className='op-msg bot'; late.id='late-plan';
          late.innerHTML='<span class="bubble">Earlier plan, delivered late</span>'; log.append(late);
        }''')
        page.wait_for_function("document.getElementById('late-plan').nextElementSibling?.hasAttribute('data-workbench-job')")
        card = page.locator('[data-workbench-job]')
        assert card.locator('.op-wb-tag').text_content() == 'Result'
        assert card.locator('.op-wb-tag-found .op-wb-tag-icon svg circle').count() == 1
        assert card.locator('table tbody tr').count() == 1
        assert card.locator('pre code').inner_text() == 'print(288)'
        assert card.locator('.op-copy').count() == 1
        assert card.locator('.op-wb-rich img').count() == 0
        for scale in (.8, 1.05, 1.6):
            typography = page.evaluate('''scale => {
              document.querySelector('.op').style.setProperty('--chat-scale',scale);
              const result=document.querySelector('[data-workbench-job]');
              return {body:parseFloat(getComputedStyle(result.querySelector('.op-wb-rich')).fontSize),
                reply:parseFloat(getComputedStyle(document.querySelector('#late-plan .bubble')).fontSize),
                title:getComputedStyle(result.querySelector('h3')).fontWeight,
                label:getComputedStyle(result.querySelector('.op-wb-tag')).textTransform};
            }''', scale)
            assert abs(typography['body'] - typography['reply']) < .1
            assert typography['title'] == '700'
            assert typography['label'] == 'none'
        page.evaluate("document.querySelector('.op').style.setProperty('--chat-scale',1.05)")
        page.evaluate('''() => {
          const user = document.createElement('div'); user.className='op-msg user'; user.id='next-turn';
          user.innerHTML='<span class="bubble">Different task</span>'; document.getElementById('op-log').append(user);
        }''')
        page.wait_for_timeout(100)
        assert page.evaluate("document.querySelector('[data-workbench-job]').nextElementSibling.id") == 'next-turn'
        page.add_style_tag(content=':root{--fg:#e7ecf3;--muted:#79828d;--border:#282a30;--border-2:#34363d} *{animation:none!important;transition:none!important}')
        page.locator('#op-lp-x').dispatch_event('click')
        card.scroll_into_view_if_needed()
        assert card.evaluate('el => el.scrollWidth <= el.clientWidth')
        page.screenshot(path='/tmp/operator-rich-result.png')
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()


@pytest.mark.parametrize("favicon_loads", [True, False], ids=["loaded", "fallback"])
def test_confirmed_order_links_only_observed_order_and_keeps_approval_history(browser, harness, favicon_loads):
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Place an order')
    proposal = {'kind': 'purchase', 'destination': 'amazon.ca',
                'description': 'One toothpaste', 'amount': 33.59, 'currency': 'CAD'}
    approval = ws.request_approval(run['run_id'], run['credential'], proposal)
    ws.decide_approval('legacy', approval['id'], approval['fingerprint'], True)
    ws.request_approval(run['run_id'], run['credential'], proposal)  # Claimed by the agent.
    number = '701-8676921-9623432'
    order_url = 'https://www.amazon.ca/gp/your-account/order-details?orderID=' + number
    ev = ws.observe(run['run_id'], 'browser_snapshot', 'Order details',
        [order_url, order_url, 'https://mail.google.com/mail/u/0/#inbox/abc',
         'https://www.amazon.ca/gp/your-account/order-history'])
    matched = ws.publish_result(run['run_id'], run['credential'], {
        'title': 'Order confirmed', 'status': 'confirmed', 'summary': 'One item.',
        'confirmation': 'Amazon.ca order ' + number, 'evidence_ids': [ev['id']]})
    fallback = ws.publish_result(run['run_id'], run['credential'], {
        'title': 'Another order', 'status': 'confirmed', 'summary': 'One item.',
        'confirmation': 'Amazon.ca order 701-1111111-2222222', 'evidence_ids': [ev['id']]})
    ws.finish_run(run['run_id'], 'done')
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page(); errors = []; harness.mode = 'live'
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    favicon_urls = tuple(
        'https://www.google.com/s2/favicons?domain=' + host + '&sz=32'
        for host in ('www.amazon.ca', 'mail.google.com'))
    page.route('https://www.google.com/s2/favicons?*', lambda route: route.abort())
    for favicon_url in favicon_urls:
        if favicon_loads:
            page.route(favicon_url, lambda route: route.fulfill(
                content_type="image/svg+xml",
                body='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                     '<circle cx="8" cy="8" r="7"/></svg>'))
        else:
            page.route(favicon_url, lambda route: route.abort())
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.add_style_tag(content=':root{--fg:#e7ecf3;--fg-2:#b3bac5;--muted:#79828d;--bg:#000;--bg-3:#12141a;--border:#292b30} body{color:var(--fg);background:#000}')
        page.evaluate("document.getElementById('op-lp').style.display='none'; document.querySelector('.op').dataset.mode='auto'")
        order = page.locator(f'[data-workbench-card="{matched["id"]}"]')
        order.wait_for(state='visible')
        assert order.locator('.op-wb-tag').inner_text() == 'Confirmed'
        assert order.locator('.op-wb-tag-confirmed .op-wb-tag-icon svg path').count() == 1
        assert order.locator('.op-wb-tag').evaluate('el => parseFloat(getComputedStyle(el).fontSize)') >= 10
        link = order.locator('.op-wb-order a')
        assert link.inner_text() == number
        assert link.get_attribute('href') == order_url
        sources = order.locator('.op-wb-sources')
        summary = sources.locator('summary')
        assert summary.text_content() == 'Sources3'
        assert summary.evaluate("el => getComputedStyle(el, '::after').content") == 'none'
        assert summary.evaluate('el => Math.abs(el.getBoundingClientRect().width - el.parentElement.getBoundingClientRect().width) < 1')
        assert not sources.locator('a').first.is_visible()
        summary.hover(position={'x': 8, 'y': 8})
        page.wait_for_function("""() => {
          const row = document.querySelector('.op-wb-sources summary');
          return row?.matches(':hover') && getComputedStyle(row).backgroundColor !== 'rgba(0, 0, 0, 0)';
        }""")
        output = os.environ.get('OPERATOR_QA_OUTPUT')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            order.screenshot(path=str(Path(output) / 'result-sources-collapsed.png'))
        summary.click(position={'x': summary.bounding_box()['width'] - 8, 'y': 8})
        assert sources.get_attribute('open') is not None
        if output:
            order.screenshot(path=str(Path(output) / 'result-sources-expanded.png'))
        assert sources.locator('a').count() == 3
        assert sources.locator('a').first.get_attribute('href') == order_url
        assert 'checked' not in sources.inner_text().lower()
        if favicon_loads:
            page.wait_for_function("""sources => {
              const images = [...sources.querySelectorAll('img')];
              return images.length === 3 && images.every(img => img.complete && img.naturalWidth > 0);
            }""", arg=sources.element_handle())
            assert sources.locator('img').count() == 3
            assert set(sources.locator('img').evaluate_all('imgs => imgs.map(img => img.src)')) == set(favicon_urls)
        else:
            page.wait_for_function("sources => sources.querySelectorAll('img').length === 0",
                                   arg=sources.element_handle())
            assert sources.locator('img').count() == 0
        assert sources.locator('a').count() == 3
        copy = page.locator(f'[data-workbench-card="{fallback["id"]}"] .op-wb-order button')
        assert copy.get_attribute('aria-label') == 'Copy order number 701-1111111-2222222'
        assert page.locator(f'[data-workbench-card="{fallback["id"]}"] .op-wb-order a').count() == 0
        approved = page.locator(f'[data-workbench-card="{approval["id"]}"]')
        assert approved.is_visible()
        assert 'Approved' in approved.inner_text()
        assert 'amazon.ca' in approved.inner_text().lower()
        assert approved.locator('button').count() == 1
        assert approved.locator('button').is_disabled()
        page.reload(wait_until='domcontentloaded')
        page.locator(f'[data-workbench-card="{approval["id"]}"]').wait_for(state='visible')
        assert errors == []
    finally:
        ctx.close()


def test_job_browser_handoff_has_no_background_return_button(browser, harness, monkeypatch):
    import operator_browser_jobs as jobs
    OS_MOD.create('Human chat')
    job = OS_MOD.create('App job', activate=False, reuse_draft=False, origin='app')['id']
    calls = []
    monkeypatch.setattr(jobs, 'reopen', lambda cid, endpoint, ensure: calls.append(('inspect', cid)))
    monkeypatch.setattr(jobs, 'park', lambda cid, endpoint: calls.append(('park', cid)) or True)
    monkeypatch.setattr(OV, '_browser_tab_command', lambda action, cid, **kw: True)
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 1280, 'height': 800})
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda error: errors.append(str(error)))
    try:
        with pg.expect_response(lambda response: response.url.endswith('/sessions/' + job + '/browser')
                                and response.request.method == 'POST'):
            pg.goto(harness.base + '/operator?conversation_id=' + job, wait_until='domcontentloaded')
        assert pg.locator('#op-background-return').count() == 0
        assert ('inspect', job) in calls and job not in OS_MOD.background_ids()
        assert ('park', job) not in calls
        assert OS_MOD.exists(job)
        assert OS_MOD.active_id() == job
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize('stale_id,method', [('legacy', 'picker'), ('deleted-chat', 'drop')])
def test_files_upload_recovers_a_deleted_cached_chat(browser, harness, stale_id, method):
    import operator_workspace as ws
    ws.delete_conversation(stale_id)
    current = OS_MOD.create('Current chat')['id']
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    ctx.add_init_script("sessionStorage.setItem('operator-conversation-v2', " + json.dumps(stale_id) + ");")
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function("id => window.OperatorWorkbenchBridge?.context().conversation_id === id && window.OperatorWorkbenchBridge.context().can_control", arg=current)
        if method == 'picker':
            page.locator('#op-files-open').dispatch_event('click')
            with page.expect_file_chooser() as picker:
                page.get_by_role('button', name='Add files', exact=True).click()
            picker.value.set_files({'name': 'fixture.txt', 'mimeType': 'text/plain', 'buffer': b'fixture upload'})
        else:
            page.locator('#op-log').evaluate("el => { const dt = new DataTransfer(); dt.items.add(new File(['fixture upload'], 'fixture.txt', {type:'text/plain'})); el.dispatchEvent(new DragEvent('drop', {bubbles:true, dataTransfer:dt})); }")
        page.get_by_role('link', name='fixture.txt', exact=True).wait_for()
        assert ws.snapshot(current)['files'][0]['name'] == 'fixture.txt'
        assert ws.snapshot(stale_id)['files'] == []
        assert page.locator('#op-files-dialog').is_visible()
        assert page.locator('#op-steering-notice').is_hidden()
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_attachment_strip_stays_one_row_and_preserves_controls(browser, harness, width):
    import io
    import operator_workspace as ws
    names = ['Annual report with a very long filename 2026.pdf', 'notes.txt', 'reference.csv']
    files = [ws.put_file('legacy', name, io.BytesIO(b'fixture')) for name in names]
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda error: errors.append(str(error)))
    pg.route('**/operator/workspace?*', lambda r: r.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.wait_for_selector('.op-wb-file', state='attached')
        pg.locator('#op-lp-x').dispatch_event('click')
        assert pg.locator('#op-files-dialog').is_hidden()
        pg.locator('#op-files-open').dispatch_event('click')
        assert pg.locator('.op-wb-file').count() == 3
        for name in names:
            assert pg.locator('#op-files-dialog').get_by_role('button', name='Remove ' + name, exact=True).count() == 1
        geometry = pg.locator('.op-wb-files').evaluate("el => ({height:el.getBoundingClientRect().height, client:el.clientWidth, scroll:el.scrollWidth, rows:[...el.children].map(c=>c.offsetTop)})")
        assert geometry['scroll'] <= geometry['client'] + 1, geometry
        link = pg.get_by_role('link', name=names[0], exact=True)
        assert link.get_attribute('download') == names[0]
        assert link.get_attribute('title') == names[0]
        assert 'PDF · 1 KB' in link.inner_text()
        pg.add_style_tag(content=':root{--fg:#e7ecf3;--muted:#7e8a9a;--border:#292b30;--bg-2:#14161c;--bg-3:#202125} body{background:#000;color:var(--fg)}')
        pg.screenshot(path='/tmp/operator-files-' + str(width) + '.png', full_page=True)
        assert not errors, errors
    finally:
        for file in files:
            ws.delete_file('legacy', file['id'])
        ctx.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_completed_reply_is_not_replayed_after_followup_but_next_run_can_repeat_it(browser, harness, width):
    harness.mode = 'live'
    session = {'log': '<div class="op-msg user"><div class="bubble">First request</div></div>',
               'mode': 'auto', 'bot': '', 'model': '', 'effort': ''}
    snapshot = {'state': 'done', 'final': '**Same answer**', 'messages': [],
                'bot': 'claude-a', 'alive': False, 'started_ts': 1, 'ended_ts': 2,
                'run_id': 'first-run'}
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    ctx.add_init_script(_seed_once_script(session))
    pg = ctx.new_page()
    pg.route('**/operator/session', lambda r: r.fulfill(json={
        'ok': True, 'data': session, 'rev': 1, 'conversation_rev': 1, 'conversation_id': 'legacy'}))
    pg.route('**/operator/session?*', lambda r: r.fulfill(status=204) if 'after_rev=' in r.request.url else r.fulfill(json={
        'ok': True, 'data': session, 'rev': 1, 'conversation_rev': 1, 'conversation_id': 'legacy'}))
    pg.route('**/operator/agent?*', lambda r: r.fulfill(json=snapshot))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        bubbles = pg.locator('.op-msg.bot .bubble').filter(has_text='Same answer')
        bubbles.wait_for(timeout=8000)
        assert pg.locator('.op-msg.bot').get_attribute('data-agent-run') == 'first-run'
        pg.evaluate("""() => {
          const user=document.createElement('div'); user.className='op-msg user';
          user.innerHTML='<div class="bubble">Follow-up request</div>';
          document.getElementById('op-log').appendChild(user);
        }""")
        pg.wait_for_timeout(1800)
        assert bubbles.count() == 1, 'old completed run replayed underneath the follow-up'
        snapshot.update(run_id='second-run', started_ts=3, ended_ts=4)
        pg.wait_for_function("document.querySelectorAll('.op-msg.bot').length === 2", timeout=8000)
        assert bubbles.count() == 2, 'a genuine new run may return the same answer'
        assert pg.locator('.op-msg.bot').nth(1).get_attribute('data-agent-run') == 'second-run'
    finally:
        ctx.close()


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('source', ['generated', 'download:fixture'])
def test_generated_handoff_preview_and_download(browser, harness, width, source):
    import io
    import operator_workspace as ws
    asset = ws.put_file('legacy', 'emulator-handoff.md',
                        io.BytesIO(b'# Emulator handoff\nUse controller input.\n<script>window.previewUnsafe=true</script>'),
                        source=source)
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda e: errors.append(str(e)))
    pg.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.wait_for_selector('#op-files-dialog .op-wb-file', state='attached')
        pg.locator('#op-lp-x').dispatch_event('click')
        assert pg.locator('#op-files-dialog').is_hidden()
        pg.locator('#op-files-open').dispatch_event('click')
        pg.locator('#op-files-dialog .op-wb-file-link').click()
        pg.locator('#op-file-preview .op-md-h').filter(has_text='Emulator handoff').wait_for()
        assert pg.locator('#op-file-preview').is_visible()
        assert pg.locator('#op-file-preview .op-wb-preview-download').get_attribute('download') == 'emulator-handoff.md'
        assert pg.evaluate('window.previewUnsafe || false') is False
        pg.locator('#op-file-preview .op-wb-close').click()
        pg.evaluate("""id => {
            const message = document.createElement('div');
            message.className = 'op-msg bot';
            message.textContent = '[Read handoff](operator-file:' + id + ')';
            window.OperatorWorkbenchBridge.renderMarkdown(message);
            document.getElementById('op-log').append(message);
        }""", asset['id'])
        pg.locator('[data-operator-file]').dispatch_event('click')
        pg.locator('#op-file-preview .op-md-h').filter(has_text='Emulator handoff').wait_for()
        assert pg.locator('#op-file-preview').evaluate('e=>e.getBoundingClientRect().width') <= width
        pg.add_style_tag(content=':root{--fg:#e7ecf3;--muted:#7e8a9a;--border:#292b30;--bg-2:#14161c;--bg-3:#202125} body{background:#000;color:var(--fg)}')
        pg.screenshot(path='/tmp/operator-handoff-preview-' + str(width) + '.png', full_page=True)
        assert not errors
    finally:
        ws.delete_file('legacy', asset['id'])
        ctx.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_job_indicator_hides_empty_and_shows_actual_progress(browser, harness, width):
    import operator_workspace as ws
    job = ws.new_job('legacy', '')
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function('!!window.OperatorWorkbench')
        page.locator('#op-lp-x').dispatch_event('click')
        page.wait_for_selector('.op-job-goal', state='attached')
        assert page.evaluate('window.OperatorWorkbench.toggleJob()') is False
        assert page.locator('#op-job-panel').is_hidden()
        run = ws.start_run('legacy', 'Prepare the emulator handoff')
        ws.update_job(run['run_id'], run['credential'], {'checkpoints': [
            {'step': 'Reproduce the input issue', 'status': 'completed'},
            {'step': 'Write the handoff', 'status': 'inProgress'}]})
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function("document.querySelector('.op-job-goal')?.textContent === 'Prepare the emulator handoff'", timeout=10000)
        page.evaluate('window.OperatorWorkbench.toggleJob()')
        assert page.locator('#op-job-panel').is_visible()
        assert page.locator('.op-job-progress').inner_text() == '1/2'
        assert page.locator('#op-job-panel').get_by_role('button', name='Activity', exact=True).count() == 0
        assert page.locator('#op-job-panel').evaluate('e=>getComputedStyle(e).borderRadius') != '0px'
        assert page.locator('.op-job-checklist li').count() == 2
        page.get_by_role('button', name='Hide checklist', exact=True).click()
        assert page.locator('#op-job-panel').is_hidden()
        page.evaluate('window.OperatorWorkbench.toggleJob()')
        assert page.locator('#op-job-panel').is_visible()
        page.add_style_tag(content=':root{--fg:#e7ecf3;--muted:#7e8a9a;--border:#292b30;--bg-2:#14161c;--bg-3:#202125} body{background:#000;color:var(--fg)}')
        page.screenshot(path='/tmp/operator-job-' + str(width) + '.png', full_page=True)
        assert not errors
        ws.finish_run(run['run_id'], 'done')
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [390, 820, 1440])
def test_status_and_observer_notice_keep_empty_chat_in_flow(browser, harness, width, tmp_path):
    """Collapsed status cannot cover a notice or reserve space in an empty log."""
    ctx = browser.new_context(viewport={"width": width, "height": 1000})
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="attached", timeout=8000)
        pg.add_style_tag(content="* { transition: none !important; animation: none !important; }")
        for collapsed, observing in [(True, True), (True, False), (False, True), (False, False)]:
            geometry = pg.evaluate("""([collapsed, observing]) => {
              const op = document.getElementById('op');
              op.classList.remove('op-booting'); op.classList.add('op-ready');
              op.dataset.mode = 'auto'; op.dataset.sheet = 'half';
              op.dataset.statusMin = collapsed ? '1' : '0';
              op.style.setProperty('--sheet-h', '650px');
              document.getElementById('op-lp').hidden = true;
              document.getElementById('op-man-note').hidden = true;
              const notice = document.getElementById('op-thread-observer');
              notice.hidden = !observing;
              document.getElementById('op-thread-observer-text').textContent =
                'MacBook Air M2 has control';
              document.getElementById('op-log').replaceChildren();
              const rect = selector => document.querySelector(selector).getBoundingClientRect().toJSON();
              return {status: rect('#op-action'), notice: rect('#op-thread-observer'),
                log: rect('#op-log'), rail: rect('.op-rail'), head: rect('.op-head'),
                button: rect('#op-thread-takeover')};
            }""", [collapsed, observing])
            status, notice, log = (geometry[key] for key in ("status", "notice", "log"))
            assert status["top"] >= geometry["head"]["bottom"], geometry
            previous = status
            if observing:
                assert 0 <= notice["top"] - status["bottom"] <= 14, geometry
                assert abs(notice["left"] - status["left"]) <= 2, geometry
                assert geometry["button"]["right"] <= notice["right"], geometry
                previous = notice
            assert 0 <= log["top"] - previous["bottom"] <= 16, geometry
            assert log["height"] > 0, geometry
            assert log["right"] <= geometry["rail"]["right"], geometry
            if collapsed:
                pg.screenshot(path=str(tmp_path / f"status-{width}-observer-{observing}.png"))
    finally:
        ctx.close()


@pytest.mark.parametrize('width', [390, 820, 1440])
def test_chat_file_pills_and_context_manager(browser, harness, width, tmp_path):
    import io
    import operator_workspace as ws
    for i in range(5):
        ws.put_file('file-manager', f'project-notes-{i}.txt', io.BytesIO(f'Context {i}'.encode()))
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', " + json.dumps(json.dumps(_SEEDED_SESSION)) + ");")
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('file-manager'))))
    def file_route(route):
        fid = route.request.url.split('/files/')[1].split('?')[0].split('/')[0]
        if route.request.method == 'PATCH':
            body = route.request.post_data_json
            result = ws.update_file('file-manager', fid, {k: body[k] for k in ('name', 'included') if k in body})
            route.fulfill(json={'ok': True, 'file': result})
        elif route.request.method == 'DELETE':
            ws.delete_file('file-manager', fid); route.fulfill(json={'ok': True})
        elif '/preview' in route.request.url:
            path, asset = ws.file_path('file-manager', fid)
            route.fulfill(json={'ok': True, 'text': path.read_text(), 'markdown': False})
        else:
            route.continue_()
    page.route('**/operator/workspace/files/**', file_route)
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.add_style_tag(content=':root {--fg:#e7ecf3;--fg-2:#c0c6cf;--muted:#7e8a9a;--border:#292b30;--border-2:#30343b;--bg-2:#101216;--accent:#3fb950} body {color:var(--fg);background:#000}')
        page.wait_for_selector('#op-file-pills .op-file-more', state='attached')
        pills = page.locator('#op-file-pills')
        assert 2 <= pills.locator('.op-file-pill').count() <= 4
        assert pills.locator('.op-file-pill-remove').count() == pills.locator('.op-file-pill-shell').count()
        assert pills.locator('.op-file-more').inner_text().startswith('+')
        assert pills.evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        assert pills.locator('.op-file-pill-icon svg').count() > 0
        assert pills.locator('.op-file-pill').first.evaluate('e=>e.getBoundingClientRect().width') < pills.evaluate('e=>e.getBoundingClientRect().width')
        pills.locator('.op-file-pill').first.dispatch_event('click')
        page.wait_for_selector('#op-file-preview[open]')
        page.wait_for_function("document.querySelector('.op-wb-preview-body').textContent.includes('Context')")
        page.locator('#op-file-preview').get_by_role('button', name='Chat files', exact=True).click()
        manager = page.locator('#op-files-dialog')
        assert manager.locator('.op-wb-file').count() == 5
        assert manager.get_by_role('checkbox').count() == 0
        assert manager.get_by_label('Filter files', exact=True).count() == 0
        manager.get_by_label('Search files', exact=True).fill('project-notes-4')
        manager.get_by_role('button', name='Rename', exact=True).click()
        manager.get_by_label('Filename', exact=True).fill('requirements.txt')
        manager.get_by_role('button', name='Save', exact=True).click()
        manager.get_by_label('Search files', exact=True).fill('requirements')
        page.wait_for_selector('#op-files-dialog a[aria-label="requirements.txt"]')
        assert manager.locator('.op-wb-file').count() == 1
        manager.get_by_label('Search files', exact=True).fill('not-present')
        assert 'No matching files.' in manager.inner_text()
        manager.get_by_label('Search files', exact=True).fill('')
        assert manager.evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        box = manager.bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
        assert manager.locator('.op-wb-file small').first.evaluate('e=>parseFloat(getComputedStyle(e).fontSize)') >= 14
        assert manager.locator('.op-wb-file-download').first.evaluate('e=>parseFloat(getComputedStyle(e).borderTopWidth)') == 1
        manager.screenshot(path=f'/tmp/operator-files-modern-{width}.png')
        manager.get_by_role('button', name='Close', exact=True).click()
        pills.locator('.op-file-pill-remove').first.click()
        page.wait_for_function("document.querySelector('#op-files-dialog .op-wb-storage').textContent.startsWith('4 files')")
        page.screenshot(path=str(tmp_path / f'files-{width}.png'))
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('terminal', ['error', 'interrupted'])
def test_synced_fast_failure_settles_without_running_poll(browser, harness, width, terminal):
    harness.mode = 'live'
    session = {'log': '<div class="op-msg user"><div class="bubble">Continue</div></div>'
        '<div class="op-task" data-busy="1"><div class="op-task-head"><span class="ico"></span>'
        '<span class="verb">Working…</span><span class="car"></span></div>'
        '<div class="op-task-steps"></div></div>', 'mode': 'auto', 'bot': '', 'model': '', 'effort': ''}
    snapshot = {'state': terminal, 'final': '', 'messages': [], 'bot': 'claude-a',
                'alive': False, 'started_ts': 1, 'ended_ts': 2, 'run_id': 'fast-failure'}
    if terminal == 'error':
        snapshot['messages'] = [{'role': 'error', 'text': 'launch failed: missing table', 'ts': 2}]
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    ctx.add_init_script(_seed_once_script(session))
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda error: errors.append(str(error)))
    pg.route('**/operator/session?*', lambda r: r.fulfill(status=204) if 'after_rev=' in r.request.url else r.fulfill(json={
        'ok': True, 'data': session, 'rev': 1, 'conversation_rev': 1, 'conversation_id': 'legacy'}))
    pg.route('**/operator/agent?*', lambda r: r.fulfill(json=snapshot))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.wait_for_function("document.querySelector('.op-task').classList.contains('op-task-failed')", timeout=8000)
        assert pg.locator('.op-task').count() == 1
        assert pg.locator('.op-task').get_attribute('data-busy') == '0'
        label = pg.locator('.op-task .verb').inner_text()
        assert ('Failed' if terminal == 'error' else 'Interrupted') in label
        assert 'Working' not in label
        pg.wait_for_timeout(1800)
        assert pg.locator('.op-task').count() == 1
        assert not errors, errors
    finally:
        ctx.close()


def test_token_counter_uses_shared_rolling_digits(browser, harness):
    harness.agent_mode = 'running'
    harness.agent_messages = [{'ts': 2_000_000_002, 'role': 'action', 'text': 'Reading', 'detail': ''}]
    harness.agent_in_tokens = 48127
    harness.agent_out_tokens = 913
    base = Path(__file__).resolve().parents[2] / 'host-app'
    host = (base / 'templates/_base.html').read_text()
    roller = host[host.index('function paintRolling('):host.index('</script>', host.index('function paintRolling('))]
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    ctx.add_init_script(roller + "\nwindow.paintRolling = paintRolling;")
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page()
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.add_style_tag(content=(base / 'static/css/base.css').read_text())
        page.wait_for_function("document.querySelector('#op-token-in')?.getAttribute('aria-label') === '48.1k'")
        harness.agent_in_tokens = 48927
        page.wait_for_function("document.querySelector('#op-token-in .rolling') !== null")
        page.wait_for_function("document.querySelector('#op-token-in')?.textContent === '48.9k'")
        assert page.locator('#op-token-in').get_attribute('aria-label') == '48.9k'
        harness.agent_out_tokens = 1000
        page.wait_for_function("document.querySelector('#op-token-out')?.textContent === '1k'")
        assert page.locator('#op-token-out').get_attribute('aria-label') == '1k'
    finally:
        ctx.close()


def test_completed_turn_retains_final_tokens_after_poll_and_reload(browser, harness):
    harness.agent_mode = None
    snapshot = {'state': 'running', 'run_id': 'completed-token-fixture',
        'started_ts': 2_000_000_000, 'alive': True, 'final': '',
        'messages': [{'ts': 2_000_000_002, 'role': 'action', 'text': 'Reading', 'detail': ''}],
        'cumulative_in_tokens': 48127, 'cumulative_out_tokens': 913}
    harness.agent_messages = [{'ts': 2_000_000_002, 'role': 'action', 'text': 'Reading', 'detail': ''}]
    harness.agent_in_tokens = 48127
    harness.agent_out_tokens = 913
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    ctx.add_init_script("if (!localStorage.getItem('operator-session-v2')) localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page()
    page.route('**/operator/agent?*', lambda route: route.fulfill(json=snapshot))
    page.route('**/operator/history?*', lambda route: route.fulfill(json={'ok': True, 'turns': []}))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function("document.querySelector('#op-token-in')?.textContent === '48.1k'")
        snapshot.update(state='done', ended_ts=2_000_000_030, messages=[], alive=False,
            cumulative_in_tokens=48927, cumulative_out_tokens=1000)
        meter = page.locator('.op-task[data-busy="0"] .op-token-meter')
        meter.wait_for(state='attached')
        assert not meter.is_visible()
        page.locator('.op-task[data-busy="0"] .op-task-head').click()
        meter.wait_for(state='visible')
        assert meter.locator('.op-token-in').text_content() == '↑48.9k'
        assert meter.locator('.op-token-out').text_content() == '↓1k'
        assert meter.get_attribute('title') == 'Input: 48,927 tokens · Output: 1,000 tokens'
        assert meter.locator('[id]').count() == 0
        page.wait_for_timeout(1800)
        assert meter.is_visible()
        # Wait for the collapsed snapshot's server acknowledgement; 400ms
        # was shorter than the 600ms save debounce, and reload re-seeded the
        # cache before the pending pagehide write had necessarily arrived.
        with page.expect_response(lambda response:
                '/operator/session' in response.url and response.request.method == 'POST'
                and 'collapsed' in (response.request.post_data_json or {}).get('data', {}).get('log', '')) as saved:
            page.locator('.op-task[data-busy="0"] .op-task-head').click()
            assert not meter.is_visible()
        assert saved.value.status == 200
        page.reload(wait_until='domcontentloaded')
        meter.wait_for(state='attached')
        assert not meter.is_visible()
        page.locator('.op-task[data-busy="0"] .op-task-head').click()
        meter.wait_for(state='visible')
        assert meter.locator('.op-token-in').text_content() == '↑48.9k'
        assert meter.locator('.op-token-out').text_content() == '↓1k'
    finally:
        ctx.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_dyson_approval_presentation_and_prepared_result_action(browser, harness, width):
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Prepare an order')
    ws.publish_result(run['run_id'], run['credential'], {'title': 'Prepared order', 'status': 'prepared', 'summary': 'Order details are prepared.'})
    approval = ws.request_approval(run['run_id'], run['credential'], {
        'kind': 'purchase', 'destination': 'dyson canada — https://www.dysoncanada.ca/en/checkout',
        'description': 'Place one order.', 'amount': 471.90, 'currency': 'CAD'})
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page(); harness.mode = 'live'
    page.route('**/operator/workspace?*', lambda r: r.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    page.route('https://www.google.com/s2/favicons?*', lambda r: r.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7"/></svg>'))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.add_style_tag(content=':root{--fg:#e7ecf3;--muted:#7e8a9a;--border:#292b30;--border-2:#30343b;--bg:#000} body{color:var(--fg);background:#000}')
        page.locator('#op-lp-x').dispatch_event('click')
        card = page.locator('.op-wb-approval'); card.wait_for()
        assert card.locator('.op-wb-merchant-row').inner_text() == 'Dyson Canada'
        assert '—' not in card.locator('.op-wb-merchant').inner_text()
        assert 'dysoncanada.ca' in card.locator('.op-wb-merchant-icon').get_attribute('src')
        assert card.locator('.op-wb-checkout-link').get_attribute('href') == 'https://www.dysoncanada.ca/en/checkout'
        assert card.locator('.op-wb-approval-actions > button').all_text_contents() == ['Approve', 'Decline', 'Request changes']
        assert card.locator('.op-wb-approval-actions .op-wb-primary').evaluate('e=>getComputedStyle(e).backgroundColor') == 'rgb(72, 200, 101)'
        assert card.locator('.op-wb-decline').evaluate('e=>getComputedStyle(e).color') == 'rgb(247, 120, 120)'
        assert card.locator('.op-wb-currency-symbol').inner_text() == '$'
        assert card.locator('.op-wb-amount-value').evaluate('e=>getComputedStyle(e).fontWeight') == '650'
        assert card.locator('.op-wb-currency-symbol').evaluate('e=>parseFloat(getComputedStyle(e).fontSize)') < card.locator('.op-wb-amount-value').evaluate('e=>parseFloat(getComputedStyle(e).fontSize)')
        assert card.locator('.op-wb-currency-symbol').evaluate('e=>getComputedStyle(e).color') == 'rgb(126, 138, 154)'
        assert card.locator('.op-wb-amount-value').inner_text() == '$471.90'
        assert card.locator('.op-wb-amount-value').evaluate('e=>parseFloat(getComputedStyle(e).fontSize)') > card.locator('.op-wb-approval-heading').evaluate('e=>parseFloat(getComputedStyle(e).fontSize)')
        page.wait_for_function("document.querySelector('.op-wb-approval-actions .op-wb-primary')?.disabled === false")
        review = page.get_by_role('button', name='Review approval', exact=True); review.click()
        assert page.locator('.op-wb-approval-actions .op-wb-primary').evaluate('e=>e===document.activeElement')
        assert card.locator('.op-wb-merchant-icon').evaluate('e=>getComputedStyle(e).backgroundColor') == 'rgb(255, 255, 255)'
        page.wait_for_function("() => { const h=document.querySelector('.op-wb-approval-heading'); return h.lastElementChild.getBoundingClientRect().right <= h.getBoundingClientRect().right + 1; }")
        card.screenshot(path='/tmp/operator-approval-modern-' + str(width) + '.png')
        ws.decide_approval('legacy', approval['id'], approval['fingerprint'], True)
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function("document.querySelector('.op-wb-tag-prepared')?.textContent === 'Prepared'")
        assert page.get_by_role('button', name='Review approval', exact=True).count() == 0
        page.wait_for_function("getComputedStyle(document.querySelector('.op-wb-approval.is-approved .op-wb-approval-heading')).color === 'rgb(72, 200, 101)'")
    finally:
        ctx.close()


@pytest.mark.parametrize('race', [False, True])
def test_followup_uses_server_active_run_when_local_state_is_idle(browser, harness, race):
    harness.mode = 'live'
    ctx = browser.new_context()
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'<div class=\"op-msg bot\"><div class=\"bubble\">Previous reply</div></div>',mode:'auto',bot:'gpt'}))")
    page = ctx.new_page(); corrections = []; dispatches = []
    idle = {'state': 'idle', 'messages': [], 'alive': False, 'run_id': ''}
    active = {'state': 'running', 'messages': [], 'alive': True, 'run_id': 'sonnet-active', 'bot': 'claude-a'}
    page.route('**/operator/agent?*', lambda r: r.fulfill(json=idle if 'since=' in r.request.url or race else active))
    page.route('**/operator/dispatch', lambda r: (dispatches.append(r.request.post_data_json), r.fulfill(status=409, json={
        'ok': False, 'code': 'conversation_running', 'run_id': 'sonnet-active', 'active_bot': 'claude-a', 'error': 'Already running'})))
    page.route('**/operator/agent/say', lambda r: (corrections.append(r.request.post_data_json), r.fulfill(json={'ok': True})))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function("document.getElementById('op').dataset.threadControl === 'controller'")
        page.locator('#op-lp-x').dispatch_event('click')
        page.fill('#op-input', 'Use the saved checkout details')
        page.press('#op-input', 'Enter')
        page.wait_for_function("document.querySelector('.op-msg.user.op-held') !== null")
        page.wait_for_timeout(900)
        assert len(corrections) == 1
        assert corrections[0]['run_id'] == 'sonnet-active'
        assert corrections[0]['text'] == 'Use the saved checkout details'
        assert corrections[0]['message_id']
        assert len(dispatches) == int(race)
        assert page.locator('.op-msg.user').count() == 1
        assert 'Already running' not in page.locator('#op-log').inner_text()
        assert harness.stop_posts == []
    finally:
        ctx.close()


def test_png_image_preview_renders_downloadable_image(browser, harness):
    import io
    import operator_workspace as ws
    from PIL import Image
    png = io.BytesIO(); Image.new('RGB', (320, 180), '#438fca').save(png, format='PNG')
    raw = png.getvalue(); asset = ws.put_file('png-preview', 'image.png', io.BytesIO(raw))
    harness.mode = 'live'; ctx = browser.new_context(viewport={'width': 820, 'height': 900})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', " + json.dumps(json.dumps(_SEEDED_SESSION)) + ");")
    page = ctx.new_page()
    page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('png-preview'))))
    def image_route(route):
        if '/preview' in route.request.url: route.fulfill(json={'ok': True, 'image': True})
        else: route.fulfill(content_type='image/png', headers={'Content-Disposition': 'attachment; filename="image.png"'}, body=raw)
    page.route('**/operator/workspace/files/**', image_route)
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function("document.getElementById('op-lp-x')._wired === true")
        page.locator('#op-lp-x').dispatch_event('click')
        page.locator('#op-file-pills .op-file-pill').click()
        page.wait_for_function("document.querySelector('#op-file-preview img')?.naturalWidth === 320")
        link = page.locator('.op-wb-preview-download')
        assert link.get_attribute('download') == 'image.png'
        assert link.evaluate('e=>parseFloat(getComputedStyle(e).borderTopWidth)') == 1
        assert link.evaluate('e=>getComputedStyle(e).backgroundColor') != 'rgba(0, 0, 0, 0)'
    finally:
        ctx.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_turn_token_row_stays_visible_and_resets_between_turns(browser, harness, width, tmp_path):
    harness.agent_mode = 'off'
    snapshot = {'state': 'running', 'run_id': 'tokens-first', 'started_ts': 2_000_000_000,
        'messages': [{'ts': 2_000_000_002, 'role': 'action', 'text': 'Reading', 'detail': ''}],
        'cumulative_in_tokens': 409127, 'cumulative_out_tokens': 95, 'alive': True}
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page()
    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/operator/agent?*', lambda route: route.fulfill(json=snapshot))
    page.route('**/operator/history?*', lambda route: route.fulfill(json={'ok': True, 'turns': []}))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        _token_host_styles(page)
        page.wait_for_function("document.querySelector('#op-token-in')?.textContent === '409.1k'")
        row = page.locator('.op-task[data-busy="1"] .op-step-count')
        meter = row.locator('.op-token-meter')
        size = meter.evaluate('e=>parseFloat(getComputedStyle(e).fontSize)')
        assert size == pytest.approx(row.locator('.sc-n').evaluate('e=>parseFloat(getComputedStyle(e).fontSize)') * .60 / .72)
        assert meter.locator('.op-token-in > span').evaluate_all('es=>es.map(e=>getComputedStyle(e).fontSize)') == [f'{size:g}px', f'{size:g}px']
        assert meter.evaluate('e=>getComputedStyle(e).fontWeight') == '575'
        assert meter.bounding_box()['x'] + meter.bounding_box()['width'] <= row.bounding_box()['x'] + row.bounding_box()['width'] + 1
        screenshot_dir = Path(os.environ.get('OPERATOR_UI_SCREENSHOT_DIR', tmp_path))
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        page.locator('.op-task').screenshot(path=str(screenshot_dir / f'tokens-live-{width}.png'))
        head = page.locator('.op-task[data-busy="1"] .op-task-head')
        head.click()
        assert not row.is_visible()
        assert not meter.is_visible()
        page.wait_for_timeout(1800)  # Usage polling must preserve trace visibility.
        assert not row.is_visible()
        head.click()
        assert row.is_visible()
        assert meter.is_visible()
        snapshot.update(state='done', alive=False, ended_ts=2_000_000_030,
            cumulative_in_tokens=410000, cumulative_out_tokens=1000, final='First done', messages=[])
        done = page.locator('.op-task[data-busy="0"]')
        done.wait_for()
        assert not done.locator('.op-step-count').is_visible()
        done.screenshot(path=str(screenshot_dir / f'tokens-collapsed-{width}.png'))
        done.locator('.op-task-head').click()
        assert done.locator(':scope > .op-step-count .op-token-meter').is_visible()
        assert done.locator('.op-token-in').text_content() == '↑410k'
        assert done.locator('.op-token-out').text_content() == '↓1k'
        page.wait_for_timeout(400)
        # A collapsed parent can have a "visible" child box which is fully clipped.
        assert done.locator('.op-step-count').evaluate("e=>getComputedStyle(e.parentElement).opacity !== '0' && !e.closest('.op-task-steps')")
        done.screenshot(path=str(screenshot_dir / f'tokens-complete-{width}.png'))
        snapshot.update(state='running', alive=True, run_id='tokens-second', started_ts=2_000_000_040,
            final='', messages=[{'ts': 2_000_000_041, 'role': 'action', 'text': 'Clicking', 'detail': ''}],
            cumulative_in_tokens=None, cumulative_out_tokens=None)
        page.wait_for_function("document.querySelectorAll('.op-task[data-busy=\"1\"]').length === 1")
        assert page.locator('.op-task[data-busy="1"] .op-token-meter:not([hidden])').count() == 0
        assert done.locator('.op-token-in').text_content() == '↑410k'
        snapshot.update(cumulative_in_tokens=None, cumulative_out_tokens=7)
        page.wait_for_function("document.querySelector('#op-token-out')?.textContent === '7'")
        assert page.locator('#op-token-in').text_content() == '—'
        assert page.locator('.op-task .op-token-meter:not([hidden])').count() == 2
        assert page.locator('#op-log #op-token-meter').count() == 1
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('cache_log', ['', '<div class="op-msg user"><span class="bubble">Second question</span></div>'])
def test_retained_turn_usage_rebuilds_without_html_cache(browser, harness, width, cache_log, tmp_path):
    harness.agent_mode = 'off'
    session = {'mode': 'auto', 'log': cache_log}
    turns = [{'id': 1, 'task': 'First question', 'final': 'First answer', 'state': 'done',
        'step_count': 4, 'cumulative_in_tokens': 409127, 'cumulative_out_tokens': 95},
        {'id': 2, 'task': 'Second question', 'final': 'Second answer', 'state': 'done',
        'step_count': 1, 'cumulative_in_tokens': 1200, 'cumulative_out_tokens': None},
        {'id': 3, 'task': 'Unreported question', 'final': 'Unreported answer', 'state': 'done',
        'step_count': 0, 'cumulative_in_tokens': None, 'cumulative_out_tokens': None}]
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    page = ctx.new_page()
    page.route('**/operator/session', lambda route: route.fulfill(json={'ok': True, 'data': session,
        'rev': 1, 'conversation_rev': 1, 'conversation_id': 'legacy'}))
    page.route('**/operator/session?*', lambda route: route.fulfill(json={'ok': True, 'data': session,
        'rev': 1, 'conversation_rev': 1, 'conversation_id': 'legacy'}))
    page.route('**/operator/agent?*', lambda route: route.fulfill(json={'state': 'idle', 'messages': []}))
    page.route('**/operator/history?*', lambda route: route.fulfill(json={'ok': True, 'turns': turns,
        'oldest_id': 1, 'latest_id': 3, 'has_more': False}))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        _token_host_styles(page)
        first = page.locator('.op-task[data-history-turn="1"]')
        first.wait_for()
        assert first.locator('.sc-n').text_content() == '4 steps'
        assert first.locator('.op-token-in').text_content() == '↑409.1k'
        assert first.locator('.op-token-out').text_content() == '↓95'
        assert not first.locator('.op-step-count').is_visible()
        second = page.locator('.op-task[data-history-turn="2"]')
        assert second.locator('.sc-n').text_content() == '1 step'
        assert second.locator('.op-token-out').text_content() == '↓—'
        assert page.locator('.op-task[data-history-turn="3"] .op-token-meter').count() == 0
        assert page.locator('#op-log .op-token-meter').count() == 2
        screenshot_dir = Path(os.environ.get('OPERATOR_UI_SCREENSHOT_DIR', tmp_path))
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        first.screenshot(path=str(screenshot_dir / f'tokens-history-{width}.png'))
        for rev in (2, 3):
            page.evaluate('args => window._opApplySession(args[0], args[1], true, "legacy", args[1])', [session, rev])
            first.wait_for()
            assert page.locator('#op-log .op-token-meter').count() == 2
        page.reload(wait_until='domcontentloaded')
        first.wait_for()
        assert page.locator('#op-log .op-token-meter').count() == 2
        assert first.locator('.op-token-in').text_content() == '↑409.1k'
        assert page.locator('.op-task[data-history-turn="2"] .op-token-out').text_content() == '↓—'
    finally:
        ctx.close()


@pytest.mark.parametrize('state,pending,held', [
    ('running', 1, 2), ('running', 0, 0),
    ('done', 0, 0), ('error', 1, 0), ('interrupted', 1, 0),
])
def test_restored_corrections_follow_server_delivery_state(browser, harness, state, pending, held):
    """Synced/restored HTML has no local steer reference; reconcile every bubble."""
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    saved = {'mode': 'auto', 'log': (
        '<div class="op-msg user op-held"><span class="bubble">First correction</span></div>'
        '<div class="op-msg user op-held"><span class="bubble">Second correction</span></div>')}
    ctx.add_init_script('localStorage.setItem("operator-session-v2", ' + json.dumps(json.dumps(saved)) + ')')
    page = ctx.new_page()
    telemetry = {'state': 'running', 'alive': True, 'run_id': 'correction-run',
                 'steer_pending': 2, 'messages': []}
    page.route('**/operator/agent?*', lambda r: r.fulfill(json=telemetry))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.locator('#op-lp-x').dispatch_event('click')
        page.wait_for_function("document.querySelectorAll('.op-msg.user.op-held').length === 2")
        telemetry.update(state=state, alive=state == 'running', steer_pending=pending)
        page.wait_for_function('(n) => document.querySelectorAll(".op-msg.user.op-held").length === n', arg=held)
        assert page.locator('.op-msg.user').count() == 2
        if not held:
            assert page.locator('.op-msg.user').first.evaluate('(el) => getComputedStyle(el).position') != 'sticky'
            assert page.locator('.op-msg.user .bubble').first.evaluate('(el) => getComputedStyle(el).opacity') == '1'
    finally:
        ctx.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_completed_turn_accepts_late_token_usage(browser, harness, width):
    harness.mode = 'live'
    snapshot = {'state': 'running', 'run_id': 'late-token-fixture',
        'started_ts': 2_000_000_000, 'alive': True, 'final': '',
        'messages': [{'ts': 2_000_000_002, 'role': 'action', 'text': 'Reading', 'detail': ''}],
        'cumulative_in_tokens': None, 'cumulative_out_tokens': None}
    ctx = browser.new_context(viewport={'width': width, 'height': 844})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page()
    page.route('**/operator/agent?*', lambda r: r.fulfill(json=snapshot))
    page.route('**/operator/history?*', lambda r: r.fulfill(json={'ok': True, 'turns': []}))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_selector('.op-task[data-busy="1"]')
        snapshot.update(state='done', alive=False, messages=[], final='Finished')
        done = page.locator('.op-task[data-busy="0"]')
        done.wait_for()
        done.locator('.op-task-head').click()
        snapshot.update(cumulative_in_tokens=48127, cumulative_out_tokens=913)
        page.wait_for_function("document.querySelector('.op-task[data-busy=\"0\"] .op-token-meter')?.title === 'Input: 48,127 tokens · Output: 913 tokens'")
        assert done.locator('.op-token-meter').is_visible()
        assert done.locator('.op-token-in').text_content() == '↑48.1k'
        assert done.locator('.op-token-out').text_content() == '↓913'
    finally:
        ctx.close()


def test_a_result_card_folds_to_its_title_and_stays_folded(browser, harness):
    """The result under an answer collapses to its tag and title (the owner
    2026-10-19); the fold is remembered per result across a reload."""
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Compare rates')
    ev = ws.observe(run['run_id'], 'browser_snapshot', 'Rates', ['https://example.com'])
    ws.publish_result(run['run_id'], run['credential'], {'title': 'Rates', 'status': 'found',
        'summary': 'Friday is $288.', 'evidence_ids': [ev['id']]})
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    page = ctx.new_page()
    page.route('**/operator/workspace?*', lambda r: r.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        card = page.locator('[data-workbench-job]')
        card.wait_for(state='attached')
        assert card.locator('.op-wb-rich').is_visible()
        assert card.locator('.op-wb-fold').get_attribute('aria-expanded') == 'true'
        # toggled by event: the mobile log scroller keeps the card outside its
        # hit-tested box until a scroll lands, which is not the behaviour under test
        card.locator('.op-wb-fold').dispatch_event('click')
        assert not card.locator('.op-wb-rich').is_visible()
        assert card.locator('.op-wb-source').count() == 0 or not card.locator('.op-wb-source').first.is_visible()
        assert card.locator('h3').is_visible() and card.locator('.op-wb-tag').is_visible()
        assert card.locator('.op-wb-fold').get_attribute('aria-expanded') == 'false'
        page.reload(wait_until='domcontentloaded')
        card = page.locator('[data-workbench-job]')
        card.wait_for(state='attached')
        assert not card.locator('.op-wb-rich').is_visible()
        card.locator('.op-wb-fold').dispatch_event('click')
        assert card.locator('.op-wb-rich').is_visible()
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()


@pytest.mark.parametrize('approved', [False, True])
def test_delivery_approval_separates_merchant_provider_and_destination(browser, harness, approved):
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Review a delivery')
    approval = ws.request_approval(run['run_id'], run['credential'], {
        'kind': 'purchase',
        'destination': "uber eats / mcdonald's (downtown) — 123 Example Street, Sample City",
        'description': 'Place the prepared order.', 'amount': 29.27, 'currency': 'CAD'})
    if approved:
        ws.decide_approval('legacy', approval['id'], approval['fingerprint'], True)
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    pg = ctx.new_page(); harness.mode = 'live'
    pg.route('**/operator/workspace?*', lambda r: r.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    pg.route('https://www.google.com/s2/favicons?*', lambda r: r.fulfill(
        content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7"/></svg>'))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        _token_host_styles(pg)
        card = pg.locator('.op-wb-approval'); card.wait_for(state='visible')
        assert card.locator('.op-wb-merchant-row').inner_text() == "McDonald's (Downtown)"
        assert card.locator('.op-wb-provider').inner_text() == 'Uber Eats'
        assert card.locator('.op-wb-destination-detail').inner_text() == '123 Example Street, Sample City'
        sources = card.locator('.op-wb-merchant-icon').evaluate_all('(els) => els.map(e => e.src)')
        assert any('mcdonalds.com' in src for src in sources)
        assert any('ubereats.com' in src for src in sources)
        heading = card.locator('.op-wb-approval-heading')
        assert heading.inner_text() == ('Approved' if approved else 'Operator needs your permission')
        amount = card.locator('.op-wb-amount-value')
        assert amount.inner_text() == '$29.27'
        baseline = amount.evaluate('''el => {
            const dot = el.querySelector('.op-wb-decimal');
            const digits = document.createRange(); digits.selectNodeContents(el.lastChild);
            return {dot: dot.getBoundingClientRect().bottom,
                    digits: digits.getBoundingClientRect().bottom};
        }''')
        assert abs(baseline['dot'] - baseline['digits']) < 1, baseline
        output = os.environ.get('CC_UI_QA_OUTPUT')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            card.screenshot(path=str(Path(output) / ('approval-approved.png' if approved else 'approval-pending.png')))
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()
