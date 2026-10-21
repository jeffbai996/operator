"""Launchpad, library and composer contracts for the real operator_view page.

Run through scripts/cc_test.py with this explicit file. Shared fixtures and
isolation live in cockpit_support. Shared dependencies are operator_view,
operator_workspace, operator.js, operator_feed.js, workbench.js, operator.css
and workbench.css; their boot and state contracts span these groups.
"""

from cockpit_support import (  # noqa: F401
    OS_MOD,
    OV,
    Path,
    _RELOADED,
    _SEEDED_LOG,
    _SEEDED_SESSION,
    _STATUS_DEAD,
    _STATUS_LIVE,
    _collectors,
    _expand_launchpad,
    _fresh_session_store,
    _restore_harness_env,
    _restored_ctx,
    _stage_size,
    browser,
    harness,
    json,
    page,
    pytest,
)


def test_first_size_report_is_the_settled_stage_not_the_boot_splash(browser, harness):
    """The boot splash hides the rail and URL bar for two frames, and the first
    report used to fire inside them: the remote browser was sized to the whole
    window and nothing corrected it once the rail appeared (2026-10-06)."""
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": '<div class="op-msg user"><div class="bubble">hi</div></div>',
                                 "mode": "man"})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op').dataset.state === 'live'",
            timeout=8000, polling=100)
        pg.wait_for_timeout(1200)
        settled = _stage_size(pg)
        assert harness.beacons, "no size report sent"
        assert harness.beacons[0] == settled, (harness.beacons, settled)
    finally:
        ctx.close()


def test_phone_menu_reaches_its_last_item_above_the_sheet(browser, harness):
    """The menu lived inside the browser pane's stacking context, under the
    chat sheet, so on a phone its lower half sat behind the sheet and could not
    be scrolled into reach (the owner 2026-10-06)."""
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 430, "height": 860},
                              is_mobile=True, has_touch=True)
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": '<div class="op-msg user"><div class="bubble">hi</div></div>',
                                 "mode": "man"})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function("!document.getElementById('op').classList.contains('op-booting')")
        pg.click("#op-ham-btn")
        pg.wait_for_function("!document.getElementById('op-ham-menu').hidden")
        reachable = pg.evaluate("""() => {
          const m = document.getElementById('op-ham-menu');
          const last = m.lastElementChild;
          last.scrollIntoView({block: 'nearest'});
          const r = last.getBoundingClientRect();
          const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return !!hit && last.contains(hit);
        }""")
        assert reachable, "the last menu item is covered"
        # Using the quality control inside the menu must not close the menu.
        pg.click("#op-ham-quality")
        assert not pg.evaluate("document.getElementById('op-ham-menu').hidden")
    finally:
        ctx.close()


@pytest.mark.parametrize("has_history", [False, True])
def test_observer_focus_preserves_explicit_launchpad_navigation(browser, harness, has_history):
    """Presence may finish boot, but must not undo Home or dismissal afterward."""
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    session = dict(_SEEDED_SESSION, log=_SEEDED_LOG if has_history else "")
    ctx.add_init_script("localStorage.setItem('operator-session-v2', "
                        + json.dumps(json.dumps(session)) + ");")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    pg.route("**/presence", lambda route: route.fulfill(json={
        "ok": True, "can_control": False, "controller_label": "iPad"}))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function("""() => {
          const op = document.getElementById('op');
          return op.dataset.threadControl === 'observer' && !op.classList.contains('op-booting');
        }""", timeout=7000)
        # Wait for the async boot restore as well as the early presence response.
        pg.wait_for_function("document.querySelectorAll('.op-lp-card').length > 0")
        if has_history:
            pg.wait_for_selector("#op-lp", state="hidden")
            pg.locator("#op-lp-open").click()
        else:
            pg.wait_for_selector("#op-lp", state="visible")
            pg.locator("#op-lp-x").click()
        assert pg.locator("#op-lp").is_visible() == has_history

        # Entering the app can restore focus before delivering pointer movement.
        with pg.expect_response(lambda r: r.url.endswith('/presence')):
            pg.evaluate("window.dispatchEvent(new Event('focus'))")
        # An awaited heartbeat also covers the recurring five-second poll path.
        pg.evaluate("window._opThreadHeartbeat(false)")
        pg.mouse.move(1100, 450)
        assert pg.locator("#op-lp").is_visible() == has_history
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize('frames', ['running', 'suspended'])
def test_mobile_attachment_and_send_share_center_at_all_composer_sizes(browser, harness, frames):
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    # macOS Chromium stops animation frames while the display sleeps, so boot
    # ends on its timer fallback instead of the double frame (INC-0023).
    if frames == 'suspended':
        ctx.add_init_script('window.requestAnimationFrame = () => 0;')
    page = ctx.new_page()
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function('!!window.OperatorWorkbench')
        # The boot splash hides the rail until boot finishes. Interval polling,
        # because Playwright's default frame polling stalls with the frames.
        page.wait_for_function("!document.getElementById('op').classList.contains('op-booting')",
                               polling=50)
        for scale in (.8, 1.05, 1.6):
            for text in ('', 'Hello', 'A longer message\nwith several lines\nand one more line'):
                geometry = page.evaluate('''([scale,text]) => {
                  document.querySelector('.op').style.setProperty('--chat-scale', scale);
                  const input = document.getElementById('op-input'); input.value=text;
                  input.dispatchEvent(new Event('input', {bubbles:true}));
                  const a=document.getElementById('op-files-open').getBoundingClientRect();
                  const b=document.getElementById('op-send').getBoundingClientRect();
                  const icon=document.querySelector('#op-files-open svg').getBoundingClientRect();
                  return {delta:Math.abs(a.y+a.height/2-b.y-b.height/2),height:Math.abs(a.height-b.height),iconSize:icon.height,expectedIconSize:a.height-2};
                }''', [scale, text])
                assert geometry['delta'] <= .5, (scale, text, geometry)
                assert geometry['height'] <= .5, (scale, text, geometry)
                assert abs(geometry['iconSize'] - geometry['expectedIconSize']) <= .5, (scale, text, geometry)
    finally:
        ctx.close()


def test_var_task_card_prefills_composer(browser, harness):
    """1.0.13: clicking Go on a {{variable}} saved task loads the prompt into
    the composer (first placeholder selected) and fires NOTHING — no task run,
    no dispatch (the server would 400 an unfilled template anyway)."""
    import operator_tasks as OT
    slug, err = OT.save_task({"name": "Price check",
                              "prompt": "find the price of {{item}} on {{site}}"})
    assert err is None
    harness.run_posts.clear()
    harness.allowed_task_slug = None
    harness.dispatch_posts.clear()
    ctx = browser.new_context()
    # AUTO mode: the launchpad is display:none in manual (the fresh-boot default)
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
        pg.wait_for_selector(".op-lp-card", timeout=8000)
        pg.locator("#op-lp-tasks-toggle").dispatch_event("click")
        card = pg.locator(".op-lp-card", has_text="Price check").first
        card.wait_for(state="attached", timeout=8000)
        card.locator(".op-lp-go").dispatch_event("click")
        pg.wait_for_timeout(600)
        val = pg.locator("#op-input").input_value()
        assert "{{item}}" in val and "{{site}}" in val
        assert harness.run_posts == [], "var task must never auto-run"
        assert harness.dispatch_posts == []
        assert errors == [], f"JS errors: {errors}"
    finally:
        OT.delete_task(slug)
        ctx.close()


def test_launchpad_hero_dispatches_like_primary_composer(browser, harness):
    """The fresh-session hero is a real composer, not decorative chrome.

    Enter must take the exact same dispatch path as the rail composer so the
    old Operator-style homepage disappears as soon as work starts.
    """
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
        pg.wait_for_selector("#op-lp-input", state="visible", timeout=8000)
        assert pg.locator("#op-lp-wordmark").text_content() == "Operator"
        hero = pg.locator("#op-lp-input")
        hero.fill("Find two quiet hotels near Union Square")
        assert hero.input_value() == "Find two quiet hotels near Union Square"
        pg.press("#op-lp-input", "Enter")
        pg.wait_for_timeout(700)
        assert len(harness.dispatch_posts) == 1
        assert harness.dispatch_posts[0]["task"] == \
            "Find two quiet hotels near Union Square"
        assert pg.locator("#op-lp").is_hidden()
        assert pg.locator("#op-log .op-msg.user").count() == 1
        assert errors == [], f"JS errors: {errors}"
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [1200, 390])
def test_slash_picker_custom_site_on_launchpad(browser, harness, width):
    ctx = browser.new_context(viewport={"width": width, "height": 800},
                              is_mobile=width < 500, has_touch=width < 500)
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto", "bot": "",
                                 "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp-input", state="visible", timeout=8000)
        field = pg.locator("#op-lp-input")
        field.fill("Visit /example.net")
        pg.wait_for_selector(".op-resource-row", state="visible", timeout=5000)
        bounds = pg.locator("#op-resource-menu").bounding_box()
        assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1
        pg.locator(".op-resource-row", has_text="example.net").click()
        assert field.input_value() == "Visit "
        assert pg.locator('#op-hero-resources .op-resource-chip').count() == 1
        assert 'example.net' in pg.locator('#op-hero-resources').inner_text()
        assert harness.dispatch_posts == []
        assert errors == [], f"JS errors: {errors}"
    finally:
        ctx.close()


def test_launchpad_is_the_only_fresh_session_composer(browser, harness):
    """Splash mode owns the task entry surface until the first task starts.

    No cockpit chrome sits behind the homepage; Enter opens the normal flow.
    """
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
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
        pg.wait_for_selector("#op-lp-input", state="visible", timeout=8000)
        pg.wait_for_function(
            "document.getElementById('op').dataset.mode === 'auto'"
            " && document.getElementById('op').dataset.busy === '0'",
            timeout=8000, polling=100)
        assert pg.locator(".op-inputbox").evaluate(
            "el => getComputedStyle(el).display") == "none"
        assert pg.locator(".op-rail").evaluate(
            "el => getComputedStyle(el).display") == "none"
        assert pg.locator(".op-resizer").evaluate(
            "el => getComputedStyle(el).display") == "none"
        assert pg.locator(".op-urlbar").evaluate(
            "el => getComputedStyle(el).display") == "none"
        assert pg.locator("#test-site-header").evaluate(
            "el => getComputedStyle(el).display") == "none"

        pg.fill("#op-lp-input", "Open the first useful search result")
        pg.press("#op-lp-input", "Enter")
        pg.wait_for_timeout(700)
        assert pg.locator("#op-lp").is_hidden()
        assert pg.locator(".op-inputbox").evaluate(
            "el => getComputedStyle(el).display") != "none"
        assert pg.locator(".op-rail").evaluate(
            "el => getComputedStyle(el).display") != "none"
        assert pg.locator(".op-urlbar").evaluate(
            "el => getComputedStyle(el).display") != "none"
        assert pg.locator("#test-site-header").evaluate(
            "el => getComputedStyle(el).display") != "none"
        assert errors == [], f"JS errors: {errors}"
    finally:
        ctx.close()


def test_launchpad_card_copy_and_go_use_intentional_typefaces(browser, harness):
    """Card body copy is DM Sans; the compact Go action stays Jakarta."""
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        _expand_launchpad(pg)
        card = pg.locator(".op-lp-card").first
        faces = card.evaluate("""el => ({
          body: getComputedStyle(el.querySelector('.op-lp-prompt')).fontFamily,
          go: getComputedStyle(el.querySelector('.op-lp-go')).fontFamily
        })""")
        assert faces["body"].lstrip('"').startswith("DM Sans")
        assert faces["go"].lstrip('"').startswith("Plus Jakarta Sans")
    finally:
        harness.mode = "real"
        ctx.close()


def test_launchpad_wordmark_and_corner_controls_are_centered(browser, harness):
    """Rendered geometry protects the launchpad's two visible centerlines."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        _expand_launchpad(pg)
        # Geometry belongs to the settled layout. Headless Chromium can pause
        # the launchpad entrance transition when another test context held the
        # foreground, leaving the whole fixed-control coordinate space at its
        # scale(.985) starting frame indefinitely.
        pg.add_style_tag(content="#op-lp{transition:none!important;transform:none!important}")
        metrics = pg.locator("#op-lp-wordmark").evaluate(
            """el => {
              const r = el.getBoundingClientRect();
              const stage = document.getElementById('op-stage').getBoundingClientRect();
              const css = getComputedStyle(el);
              return {font: css.fontFamily, size: parseFloat(css.fontSize),
                      tracking: parseFloat(css.letterSpacing),
                      centerDelta: Math.abs((r.left + r.width / 2) -
                                           (stage.left + stage.width / 2))};
            }""")
        # 'PJS Wordmark' = the self-hosted weight-750 instance (2026-07-26);
        # Jakarta remains the fallback family.
        assert metrics["font"].startswith('"PJS Wordmark", "Plus Jakarta Sans"')
        assert 36 <= metrics["size"] <= 40
        assert metrics["tracking"] >= -0.035 * metrics["size"]
        assert metrics["centerDelta"] <= 2

        corner = pg.evaluate("""() => {
          const t = document.getElementById('op-lp-theme').getBoundingClientRect();
          const x = document.getElementById('op-lp-x').getBoundingClientRect();
          const themeCenterOffset = (t.top + t.bottom) / 2 - (x.top + x.bottom) / 2;
          return {centerDelta: Math.abs(themeCenterOffset), themeCenterOffset,
                  themeSize: t.width, closeSize: x.width, themeRight: t.right,
                  closeLeft: x.left, closeRight: x.right, viewport: innerWidth};
        }""")
        # Equal 32px controls deliberately share one centerline (the August
        # alignment fix removed the old 3px X-vs-theme mismatch). The launchpad
        # entrance uses a sub-pixel scale, so compare the rendered controls to
        # each other and allow that temporary fractional transform.
        assert corner["centerDelta"] <= 0.25
        assert abs(corner["themeSize"] - corner["closeSize"]) <= 0.25
        assert 31 <= corner["themeSize"] <= 32.5
        assert corner["themeRight"] < corner["closeLeft"]
        assert corner["viewport"] - corner["closeRight"] <= 20
    finally:
        ctx.close()


def test_chat_picker_is_launchpad_only_and_uses_the_corner_control_row(browser, harness):
    """Chats stay out of the cramped brow and open from the welcome surface."""
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        _expand_launchpad(pg)
        pg.add_style_tag(content="#op-lp{transition:none!important;transform:none!important}")

        # Switching chats is deliberately a launchpad action now. The browser
        # brow and browser hamburger both need their scarce slots back.
        assert pg.locator("#op-chats-open").count() == 0
        assert pg.locator("#op-ham-chats").count() == 0
        picker = pg.locator("#op-lp-chats")
        assert picker.is_visible()
        assert picker.get_attribute("aria-haspopup") == "dialog"
        assert picker.get_attribute("aria-expanded") == "false"

        row = pg.evaluate("""() => {
          const rect = id => document.getElementById(id).getBoundingClientRect();
          const center = r => ({x: (r.left + r.right) / 2, y: (r.top + r.bottom) / 2});
          const mark = rect('op-lp-mark'), chats = rect('op-lp-chats');
          const theme = rect('op-lp-theme'), close = rect('op-lp-x');
          const icon = document.querySelector('#op-lp-chats svg');
          return {mark: center(mark), chats: center(chats), theme: center(theme), close: center(close),
                  sizes: [mark.width, chats.width, theme.width, close.width],
                  linecap: icon.getAttribute('stroke-linecap'), linejoin: icon.getAttribute('stroke-linejoin')};
        }""")
        assert row["mark"]["x"] < row["chats"]["x"] < row["theme"]["x"] < row["close"]["x"]
        assert max(abs(row[key]["y"] - row["close"]["y"])
                   for key in ("mark", "chats", "theme")) <= 0.25
        assert max(row["sizes"]) - min(row["sizes"]) <= 0.25
        assert row["linecap"] == row["linejoin"] == "round"

        before = pg.evaluate("""() => {
          const box = sel => {
            const r = document.querySelector(sel).getBoundingClientRect();
            return [r.x, r.y, r.width, r.height];
          };
          return {hero: box('.op-lp-hero'), composer: box('.op-lp-composer')};
        }""")
        picker.dispatch_event("click")
        dialog = pg.locator("#op-chats")
        assert dialog.is_visible()
        assert dialog.get_attribute("role") == "dialog"
        assert dialog.get_attribute("aria-modal") == "true"
        assert picker.get_attribute("aria-expanded") == "true"
        geometry = pg.evaluate("""() => {
          const d = document.getElementById('op-chats').getBoundingClientRect();
          const box = sel => {
            const r = document.querySelector(sel).getBoundingClientRect();
            return [r.x, r.y, r.width, r.height];
          };
          return {
            dialogCenter: [d.x + d.width / 2, d.y + d.height / 2],
            viewportCenter: [innerWidth / 2, innerHeight / 2],
            hero: box('.op-lp-hero'), composer: box('.op-lp-composer'),
            outsideLaunchpad: !document.getElementById('op-lp').contains(
              document.getElementById('op-chats')),
            insideOperator: document.getElementById('op').contains(
              document.getElementById('op-chats')),
            focusInside: document.getElementById('op-chats').contains(
              document.activeElement)
          };
        }""")
        assert geometry["outsideLaunchpad"] is True
        assert geometry["insideOperator"] is True
        assert geometry["focusInside"] is True
        assert geometry["hero"] == pytest.approx(before["hero"], abs=0.25)
        assert geometry["composer"] == pytest.approx(before["composer"], abs=0.25)
        assert geometry["dialogCenter"] == pytest.approx(
            geometry["viewportCenter"], abs=1)

        pg.keyboard.press("Escape")
        assert not dialog.is_visible()
        assert picker.get_attribute("aria-expanded") == "false"
        assert picker.evaluate("el => document.activeElement === el") is True
    finally:
        harness.mode = "real"
        ctx.close()


def test_chat_picker_search_empty_state_and_launchpad_icon_are_visually_finished(
        browser, harness):
    """The picker should look intentional, not like a native search field in a flex accident."""
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        _expand_launchpad(pg)
        pg.add_style_tag(content="*{transition:none!important;animation:none!important}")
        # _base.html gives every otherwise-unstyled input a themed border and
        # background. Reproduce that selector here: the chat search owns one
        # visual shell, not a second native-looking field inside it.
        pg.add_style_tag(content="""
          input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=color]):not([type=file]) {
            border: 1px solid red; border-radius: 8px; background: red;
          }
        """)
        pg.locator("#op-lp-chats").dispatch_event("click")
        pg.wait_for_selector("#op-chats", state="visible", timeout=8000)

        polish = pg.evaluate("""() => {
          const dialog = document.getElementById('op-chats');
          const search = document.querySelector('.op-chat-search');
          const input = document.getElementById('op-chat-search');
          const empty = document.getElementById('op-chat-empty');
          const list = document.getElementById('op-chat-list');
          const icon = document.querySelector('#op-lp-chats svg');
          const bubble = icon.querySelector('path:first-child').getBBox();
          const bars = icon.querySelector('path:nth-child(2)');
          const sr = search.getBoundingClientRect();
          const ir = input.getBoundingClientRect();
          const er = empty.getBoundingClientRect();
          const dr = dialog.getBoundingClientRect();
          const ds = getComputedStyle(dialog);
          const is = getComputedStyle(input);
          const usableBottom = dr.bottom - parseFloat(ds.paddingBottom || 0);
          const toolsBottom = document.querySelector('.op-chat-tools').getBoundingClientRect().bottom;
          const usableCenter = (toolsBottom + usableBottom) / 2;
          return {
            appearance: is.appearance,
            webkitAppearance: is.webkitAppearance,
            inputType: input.type,
            inputRole: input.getAttribute('role'),
            inputMode: input.inputMode,
            fontFamily: is.fontFamily,
            inputBorderWidth: is.borderTopWidth,
            inputBorderRadius: is.borderRadius,
            inputBackground: is.backgroundColor,
            inputBoxShadow: is.boxShadow,
            inputCenterDelta: Math.abs((ir.top + ir.bottom - sr.top - sr.bottom) / 2),
            listDisplay: getComputedStyle(list).display,
            emptyCenterDelta: Math.abs((er.top + er.bottom) / 2 - usableCenter),
            bubbleWidth: bubble.width,
            bubbleCenterDelta: Math.abs(bubble.x + bubble.width / 2 - 10),
            interiorStrokes: (bars.getAttribute('d').match(/M/g) || []).length
          };
        }""")
        assert polish["appearance"] == "none"
        assert polish["webkitAppearance"] == "none"
        assert polish["inputType"] == "text"
        assert polish["inputRole"] == "searchbox"
        assert polish["inputMode"] == "search"
        assert polish["fontFamily"].lstrip('"').startswith("DM Sans")
        assert polish["inputBorderWidth"] == "0px"
        assert polish["inputBorderRadius"] == "0px"
        assert polish["inputBackground"] == "rgba(0, 0, 0, 0)"
        assert polish["inputBoxShadow"] == "none"
        assert polish["inputCenterDelta"] <= 0.75
        assert polish["listDisplay"] == "none"
        assert polish["emptyCenterDelta"] <= 12
        assert polish["bubbleWidth"] <= 14.5
        assert polish["bubbleCenterDelta"] <= 0.25
        assert polish["interiorStrokes"] == 2

        new_chat = pg.locator("#op-chat-new")
        new_chat.hover()
        assert new_chat.evaluate("el => getComputedStyle(el).filter") == "none"
    finally:
        harness.mode = "real"
        ctx.close()


def test_chat_library_becomes_a_phone_sheet_without_reflowing_the_launchpad(
        browser, harness):
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.bring_to_front()
        # Enter through the same Home control a phone user uses. The preceding
        # Chromium context may legitimately finish a pagehide transcript save
        # after teardown, in which case this fresh page resumes that chat and
        # the launchpad starts closed.
        pg.wait_for_function(
            "document.getElementById('op-lp-open')._wired === true",
            timeout=8000, polling=50)
        pg.locator("#op-lp-open").dispatch_event("click")
        pg.wait_for_selector("#op-lp-chats", state="visible", timeout=8000)
        pg.add_style_tag(content="*{transition:none!important}")
        rect = ("el => { const r=el.getBoundingClientRect(); "
                "return {x:r.x,y:r.y,width:r.width,height:r.height} }")
        before = pg.locator(".op-lp-composer").evaluate(rect)
        pg.locator("#op-lp-chats").dispatch_event("click")
        dialog = pg.locator("#op-chats")
        box = dialog.bounding_box()

        assert dialog.is_visible()
        assert box["x"] <= 12
        assert box["width"] >= 366
        assert box["y"] >= 20
        assert box["y"] + box["height"] >= 832
        assert pg.locator(".op-lp-composer").evaluate(rect) == pytest.approx(
            before, abs=0.25)
    finally:
        harness.mode = "real"
        ctx.close()


def test_chat_library_searches_and_manages_server_backed_threads(
        browser, harness, monkeypatch):
    older = OS_MOD.create()["id"]
    OS_MOD.save({"log": '<div class="op-msg user"><span class="bubble">older request</span></div>',
                 "preview": "Find a hotel in Kelowna", "bot": "gemma",
                 "surface": "browser"}, conversation_id=older)
    OS_MOD.title_if_unset("Kelowna hotels", older)
    newer = OS_MOD.create()["id"]
    OS_MOD.save({"log": '<div class="op-msg user"><span class="bubble">newer request</span></div>',
                 "preview": "Compare flights to Tokyo", "bot": "gpt",
                 "surface": "desktop-sandbox"}, conversation_id=newer)
    OS_MOD.title_if_unset("Tokyo flights", newer)
    monkeypatch.setattr(
        OV.operator_agent.runner, "conversation_summaries",
        lambda: {older: {"state": "running", "bot": "gemma", "alive": True}})
    # DELETE releases the conversation's Chrome tab through a helper
    # subprocess; this test is about the library, not the tab registry.
    monkeypatch.setattr(harness.mod, "_browser_tab_command", lambda *a, **kw: True)

    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    # Start this library check in its browser thread. Adopting the newest
    # sandbox thread during boot correctly hides the browser welcome surface.
    ctx.add_init_script("localStorage.setItem('operator-session-v2', "
                        + json.dumps(json.dumps({'_conversation_id': older,
                                                'mode': 'auto', 'surface': 'browser'})) + ");")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.bring_to_front()
        pg.wait_for_function(
            "document.getElementById('op-lp-open')._wired === true",
            timeout=8000, polling=50)
        pg.locator("#op-lp-open").dispatch_event("click")
        pg.wait_for_selector("#op-lp-chats", state="visible", timeout=8000)
        pg.locator("#op-lp-chats").dispatch_event("click")
        pg.wait_for_selector(".op-chat-row", state="visible", timeout=8000)

        titles = pg.locator(".op-chat-title").all_text_contents()
        assert titles == ["Kelowna hotels", "Tokyo flights"]
        assert "Find a hotel in Kelowna" in pg.locator("#op-chat-list").inner_text()
        assert "Gemma" not in pg.locator("#op-chat-list").inner_text()
        assert "gemma" in pg.locator("#op-chat-list").inner_text()

        pg.locator("#op-chat-search").fill("TOKYO")
        assert pg.locator(".op-chat-row").count() == 1
        assert pg.locator(".op-chat-title").inner_text() == "Tokyo flights"
        pg.locator("#op-chat-search").fill("")

        rows = pg.locator(".op-chat-row")
        rows.nth(0).locator(".op-chat-more").dispatch_event("click")
        assert rows.nth(0).locator(".op-chat-menu .danger").is_disabled()
        rows.nth(1).locator(".op-chat-more").dispatch_event("click")
        rows.nth(1).locator(
            ".op-chat-menu button", has_text="Rename").dispatch_event("click")
        rename = rows.nth(1).locator(".op-chat-rename input")
        # Reproduce the parent shell's generic input styling, which used to
        # override the compact rename field but not its surrounding buttons.
        pg.add_style_tag(content=".wrap input[type=text]{font-size:16px;background:#111}")
        geometry = rename.evaluate("""el => {
            const cs = getComputedStyle(el);
            const save = el.closest('form').querySelector('button[type=submit]');
            return {font: parseFloat(cs.fontSize), line: parseFloat(cs.lineHeight),
                height: el.getBoundingClientRect().height,
                buttonHeight: save.getBoundingClientRect().height,
                family: cs.fontFamily};
        }""")
        assert geometry["font"] < 14, geometry
        assert geometry["height"] - geometry["line"] >= 10, geometry
        assert abs(geometry["height"] - geometry["buttonHeight"]) < 1, geometry
        assert "DM Sans" in geometry["family"]
        rename.fill("Japan fare research")
        rows.nth(1).locator(".op-chat-rename").evaluate("form => form.requestSubmit()")
        pg.wait_for_selector("text=Japan fare research", timeout=5000)

        rows = pg.locator(".op-chat-row")
        rows.nth(1).locator(".op-chat-more").dispatch_event("click")
        rows.nth(1).locator(
            ".op-chat-menu button", has_text="Delete").dispatch_event("click")
        assert pg.locator("#op-chat-confirm").is_visible()
        pg.locator("#op-chat-delete-confirm").dispatch_event("click")
        pg.wait_for_function(
            "document.querySelectorAll('.op-chat-row').length === 1",
            timeout=5000, polling=50)
        assert errors == []
    finally:
        harness.mode = "real"
        ctx.close()


def test_chat_library_refresh_in_flight_keeps_open_delete_confirmation(
        browser, harness, monkeypatch):
    """A background list refresh that was already in flight when the user
    opened a delete confirmation must not re-render the list over it."""
    older = OS_MOD.create()["id"]
    OS_MOD.save({"log": '<div class="op-msg user"><span class="bubble">a</span></div>',
                 "preview": "first", "bot": "gemma"}, conversation_id=older)
    OS_MOD.title_if_unset("First chat", older)
    newer = OS_MOD.create()["id"]
    OS_MOD.save({"log": '<div class="op-msg user"><span class="bubble">b</span></div>',
                 "preview": "second", "bot": "gpt"}, conversation_id=newer)
    OS_MOD.title_if_unset("Second chat", newer)
    monkeypatch.setattr(OV.operator_agent.runner, "conversation_summaries", lambda: {})

    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    pg.clock.install()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op-lp-open')._wired === true",
            timeout=8000, polling=50)
        pg.locator("#op-lp-open").dispatch_event("click")
        pg.wait_for_selector("#op-lp-chats", state="visible", timeout=8000)
        pg.locator("#op-lp-chats").dispatch_event("click")
        pg.wait_for_selector(".op-chat-row", state="visible", timeout=8000)

        held = []
        pg.route("**/operator/sessions?*", lambda route: held.append(route))
        # Run the real periodic callback without waiting five wall-clock seconds.
        with pg.expect_request("**/operator/sessions?*"):
            pg.clock.run_for(5000)
        assert held, "the silent list refresh never fired"

        row = pg.locator(".op-chat-row", has_text="Second chat")
        row.locator(".op-chat-more").dispatch_event("click")
        row.locator(".op-chat-menu button", has_text="Delete").dispatch_event("click")
        assert pg.locator("#op-chat-confirm").is_visible()

        # Release the refresh with changed data, so it would re-render.
        response = held[0].fetch()
        body = response.json()
        for session in body["sessions"]:
            session["updated_ts"] = (session.get("updated_ts") or 0) + 1
        held[0].fulfill(response=response, json=body)
        pg.unroute("**/operator/sessions?*")
        pg.wait_for_timeout(300)

        assert pg.locator("#op-chat-confirm").is_visible()
        pg.locator("#op-chat-delete-confirm").dispatch_event("click")
        pg.wait_for_function(
            "document.querySelectorAll('.op-chat-row').length === 1",
            timeout=5000, polling=50)
    finally:
        harness.mode = "real"
        ctx.close()


def test_launchpad_backdrop_collapses_results_and_theme_toggle_is_local(browser, harness):
    """Empty-space clicks compact the splash; category and theme controls remain useful."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        _expand_launchpad(pg)
        # This test is about click boundaries and state, not animation timing.
        # Remove transitions so a throttled headless tab cannot strand the grid
        # halfway through its collapse.
        pg.add_style_tag(content=(
            ".op-lp-results,.op-lp,.op-lp-grid{transition:none!important}"))
        hero_top = pg.locator(".op-lp-hero").bounding_box()["y"]

        pg.mouse.click(20, 450)
        pg.wait_for_function(
            "document.querySelector('.op-lp-results-inner').getBoundingClientRect().height < 1",
            timeout=3000, polling=50)
        assert "op-lp-collapsed" in pg.locator("#op-lp").get_attribute("class")
        assert pg.locator(".op-lp-results-inner").bounding_box()["height"] < 1
        assert pg.locator(".op-lp-hero").bounding_box()["y"] > hero_top + 50
        assert pg.locator("#op-lp-input").is_visible()
        assert pg.locator(".op-lp-cats").is_visible()
        assert pg.locator(".op-lp-cat.active").count() == 0

        pg.locator('.op-lp-cat[data-category="media"]').dispatch_event("click")
        pg.wait_for_timeout(500)
        assert "op-lp-collapsed" not in pg.locator("#op-lp").get_attribute("class")
        assert pg.locator(".op-lp-card").count() > 0
        assert pg.locator('.op-lp-cat[data-category="media"]').get_attribute("aria-pressed") == "true"

        # The click-away boundary is only a healthy 24px halo around the card
        # block, not the old viewport-wide results wrapper.
        grid = pg.locator("#op-lp-grid").bounding_box()
        pg.mouse.click(grid["x"] + grid["width"] + 16, grid["y"] + 20)
        assert "op-lp-collapsed" not in pg.locator("#op-lp").get_attribute("class")
        pg.mouse.click(grid["x"] + grid["width"] + 40, grid["y"] + 20)
        pg.wait_for_timeout(500)
        assert "op-lp-collapsed" in pg.locator("#op-lp").get_attribute("class")
        assert pg.locator(".op-lp-cat.active").count() == 0

        pg.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        # 3-stop cycle: dark → OLED (the squad midnight theme) → light → dark
        pg.locator("#op-lp-theme").dispatch_event("click")
        assert pg.locator("html").get_attribute("data-theme") == "midnight"
        assert pg.evaluate("localStorage.getItem('squad_theme')") == "midnight"
        assert "op-flat" in pg.locator("#op").get_attribute("class")
        pg.locator("#op-lp-theme").dispatch_event("click")
        assert pg.locator("html").get_attribute("data-theme") == "light"
        assert pg.evaluate("localStorage.getItem('squad_theme')") == "light"
        assert "op-flat" not in pg.locator("#op").get_attribute("class")
        pg.locator("#op-lp-theme").dispatch_event("click")
        assert pg.locator("html").get_attribute("data-theme") == "dark"
        # the site header's theme switch sets midnight; the cockpit follows
        pg.evaluate("document.documentElement.setAttribute('data-theme', 'midnight')")
        pg.wait_for_function("document.getElementById('op').classList.contains('op-flat')")
    finally:
        ctx.close()


def test_header_brand_metadata_and_surface_badges_are_visually_aligned(browser, harness):
    """The version hugs the wordmark and both desktop modes stay explicit."""
    ctx = browser.new_context(viewport={"width": 1800, "height": 1000})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "<div>restored</div>", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()

    active = {"key": "desktop-sandbox"}

    def selected_surfaces(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "active": active["key"],
            "surfaces": [
                {"key": "browser", "label": "Browser", "hint": "", "available": True},
                {"key": "desktop-sandbox", "label": "Sandbox", "hint": "", "available": True},
                {"key": "desktop-real", "label": "Computer", "hint": "", "available": True,
                 "gated": True},
            ],
        }))

    pg.route("**/operator/surfaces*", selected_surfaces)
    pg.route("**/operator/agent*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "state": "idle", "surface": active["key"], "messages": [],
            "bot": "gpt", "alive": False, "stalled": False,
        })))
    pg.route("**/operator/status", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            **_STATUS_DEAD, "surface": active["key"],
        })))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op-surface-chip').textContent === 'sandbox'")
        metrics = pg.evaluate("""() => {
          const title = document.querySelector('.op-title').getBoundingClientRect();
          const version = document.querySelector('.op-ver').getBoundingClientRect();
          const chip = document.getElementById('op-surface-chip').getBoundingClientRect();
          const line = document.querySelector('.op-verline').getBoundingClientRect();
          const css = getComputedStyle(document.querySelector('.op-ver'));
          return {
            version: document.querySelector('.op-ver').textContent,
            family: css.fontFamily,
            wordmarkGap: version.top - title.bottom,
            chip: document.getElementById('op-surface-chip').textContent,
            chipCenterDelta: Math.abs((chip.top + chip.bottom) / 2 -
                                      (line.top + line.bottom) / 2),
            labelCenterDelta: (() => {
              const label = document.querySelector('.op-surface-chip-label').getBoundingClientRect();
              return Math.abs((label.top + label.bottom) / 2 -
                              (chip.top + chip.bottom) / 2);
            })(),
          };
        }""")
        assert metrics["version"] == "1.2.7"
        assert metrics["family"].startswith("Urbanist")
        assert metrics["wordmarkGap"] <= 1.5
        assert metrics["chip"] == "sandbox"
        assert metrics["chipCenterDelta"] <= 0.25
        assert metrics["labelCenterDelta"] <= 0.75

        active["key"] = "desktop-real"
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_function(
            "!document.getElementById('op-surface-chip').hidden"
            " && document.getElementById('op-surface-chip').textContent === 'computer'")
        computer = pg.locator("#op-surface-chip")
        computer_geometry = computer.evaluate("""el => {
          const probe = el.cloneNode(true);
          probe.removeAttribute('id');
          probe.style.cssText = 'position:fixed;left:0;top:0;visibility:hidden';
          document.body.appendChild(probe);
          const chip = probe.getBoundingClientRect();
          const label = probe.querySelector('.op-surface-chip-label').getBoundingClientRect();
          const result = {
            display: getComputedStyle(probe).display,
            labelCenterDelta: Math.abs((label.top + label.bottom) / 2 -
                                       (chip.top + chip.bottom) / 2),
          };
          probe.remove();
          return result;
        }""")
        assert computer_geometry["display"] == "flex"
        assert computer_geometry["labelCenterDelta"] <= 0.75
        assert computer.locator(".op-surface-chip-label").text_content() == "computer"

        active["key"] = "browser"
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op-surface-chip').hidden"
            " && document.getElementById('op-surface-chip').textContent === ''")
        browser_chip = pg.locator("#op-surface-chip")
        assert not browser_chip.is_visible()
        assert browser_chip.evaluate("el => getComputedStyle(el).display") == "none"
    finally:
        ctx.close()


def test_theme_icons_crossfade_instead_of_hard_swapping(browser, harness):
    """A theme step keeps both icons painted while one exits and one enters."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800},
                              reduced_motion="no-preference")
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp-theme", state="visible", timeout=8000)
        pg.evaluate("document.documentElement.setAttribute('data-theme', 'dark');"
                    "document.getElementById('op').classList.remove('op-flat')")
        pg.wait_for_function("""() => {
          const el = document.getElementById('op-lp-theme');
          return getComputedStyle(el.querySelector('.op-lp-theme-day')).opacity === '0'
            && getComputedStyle(el.querySelector('.op-lp-theme-oled')).opacity === '1';
        }""")
        motion = pg.locator("#op-lp-theme").evaluate("""el => {
          const styles = [...el.querySelectorAll('svg')].map(getComputedStyle);
          return styles.map(s => ({
            display: s.display,
            properties: s.transitionProperty.split(',').map(v => v.trim()),
            durations: s.transitionDuration.split(',').map(v => parseFloat(v) * 1000),
          }));
        }""")
        assert all(item["display"] != "none" for item in motion)
        assert all("opacity" in item["properties"] for item in motion)
        assert all(max(item["durations"]) >= 280 for item in motion)

        # The compact brow control shares the same always-painted icon stack.
        brow_motion = pg.locator("#op-flat").evaluate("""el =>
          [...el.querySelectorAll('svg')].map(node => ({
            display: getComputedStyle(node).display,
            properties: getComputedStyle(node).transitionProperty.split(',').map(v => v.trim()),
          }))""")
        assert all(item["display"] != "none" for item in brow_motion)
        assert all("opacity" in item["properties"] for item in brow_motion)
    finally:
        ctx.close()


def test_operator_origin_and_fullscreen_are_zoom_invariant(browser, harness):
    """Fullscreen keeps its panel frame through viewport changes (8px since
    2026-07-19 "slightly slightly wider", superseding the 6px slim frame that
    itself superseded the 1.0.23 10px spec)."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.bring_to_front()
        pg.wait_for_selector("#op-lp", state="visible", timeout=8000)
        pg.add_style_tag(content="#op-lp{transition:none!important;transform:none!important}")
        # Freeze the surface whose geometry this half of the test measures;
        # cross-device session adoption is separately covered above and may
        # legitimately switch the live page back to its server-saved mode.
        pg.evaluate("""() => {
          const op = document.getElementById('op');
          op.dataset.mode = 'auto'; op.dataset.busy = '0';
          document.getElementById('op-lp').hidden = false;
        }""")
        for width, height in ((1440, 900), (1800, 1125)):
            pg.set_viewport_size({"width": width, "height": height})
            pg.wait_for_timeout(120)
            geometry = pg.evaluate("""() => {
              const rect = selector => {
                const r = document.querySelector(selector).getBoundingClientRect();
                return {x: r.x, y: r.y, right: r.right, bottom: r.bottom};
              };
              return {inner: {w: innerWidth, h: innerHeight}, body: rect('body'),
                      op: rect('#op'), launchpad: rect('#op-lp')};
            }""")
            # The stable cockpit sits below the host header; the fixed launchpad
            # and body own viewport origin. Fullscreen #op is checked below.
            for surface in ("body", "launchpad"):
                assert abs(geometry[surface]["x"]) <= 0.5, (surface, geometry)
                assert abs(geometry[surface]["y"]) <= 0.5, (surface, geometry)
                assert abs(geometry[surface]["right"] - geometry["inner"]["w"]) <= 0.5
                assert abs(geometry[surface]["bottom"] - geometry["inner"]["h"]) <= 0.5

        pg.locator("#op-lp-x").dispatch_event("click")
        pg.evaluate("document.body.classList.add('op-full')")
        full = pg.locator("#op").evaluate("""el => {
          const r = el.getBoundingClientRect();
          const rail = el.querySelector('.op-rail').getBoundingClientRect();
          const browser = el.querySelector('.op-browser').getBoundingClientRect();
          return {x: r.x, y: r.y, right: r.right, bottom: r.bottom,
                  padding: getComputedStyle(el).padding,
                  rail: {left: rail.left, top: rail.top, bottom: rail.bottom},
                  browser: {right: browser.right, top: browser.top, bottom: browser.bottom},
                  railRadius: getComputedStyle(el.querySelector('.op-rail')).borderRadius,
                  browserRadius: getComputedStyle(el.querySelector('.op-browser')).borderRadius};
        }""")
        assert full["x"] == full["y"] == 0
        assert full["right"] == 1800 and full["bottom"] == 1125
        assert full["padding"] == "8px"
        assert full["rail"] == {"left": 8, "top": 8, "bottom": 1117}
        assert full["browser"] == {"right": 1792, "top": 8, "bottom": 1117}
        assert full["railRadius"] == "10px"
        assert full["browserRadius"] == "10px"
    finally:
        ctx.close()


def test_launchpad_controls_work_while_model_discovery_is_stalled(browser, harness):
    """A slow models endpoint cannot leave the painted welcome screen inert."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    stalled = []

    def stall_models(route):
        stalled.append(route)

    pg.route("**/operator/models?*", stall_models)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="visible", timeout=8000)
        pg.wait_for_timeout(150)
        assert stalled

        _expand_launchpad(pg)
        pg.locator(".op-lp-card").first.dispatch_event("click")
        assert pg.locator("#op-lp-input").input_value()

        pg.locator('.op-lp-cat[data-category="media"]').dispatch_event("click")
        assert pg.locator('.op-lp-cat[data-category="media"]').get_attribute(
            "aria-pressed") == "true"
        pg.wait_for_timeout(250)
        assert pg.locator(".op-lp-card").count() > 0

        pg.locator("#op-lp-x").dispatch_event("click")
        assert pg.locator("#op-lp").is_hidden()
    finally:
        for route in stalled:
            try:
                route.abort()
            except Exception:
                pass
        ctx.close()


@pytest.mark.parametrize("width", [1024, 1440])
def test_desktop_launchpad_placeholder_matches_draft_size(browser, harness, width):
    """Desktop hint copy should not be larger than the text it is replacing."""
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', "
                        + json.dumps(json.dumps(dict(_SEEDED_SESSION, log=""))) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp-input", state="visible")
        field = pg.locator("#op-lp-input")
        before = field.evaluate("""el => ({
          font: parseFloat(getComputedStyle(el, '::placeholder').fontSize),
          height: el.getBoundingClientRect().height,
          line: parseFloat(getComputedStyle(el).lineHeight)
        })""")
        field.fill("A desktop draft")
        typed_font = field.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
        assert before["font"] == pytest.approx(typed_font, abs=0.1)
        assert before["height"] >= before["line"] - 0.1
        field.fill("")
        assert field.evaluate("el => getComputedStyle(el, '::placeholder').opacity") == "0"
    finally:
        ctx.close()


def test_launchpad_composer_padding_focuses_input_without_selecting_placeholder(browser, harness):
    """Every non-button pixel in the pill focuses input; empty copy is not selectable."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="visible", timeout=8000)
        point = pg.locator(".op-lp-composer").evaluate("""el => {
          const r=el.getBoundingClientRect();
          return {x:r.right-42, y:(r.top+r.bottom)/2,
            target:(document.elementFromPoint(r.right-42,(r.top+r.bottom)/2)||{}).className};
        }""")
        assert point["target"] == "op-lp-composer"
        pg.mouse.click(point["x"], point["y"])
        assert pg.evaluate("document.activeElement.id") == "op-lp-input"
        assert pg.locator("#op-lp-input").evaluate(
            "el => getComputedStyle(el).userSelect") == "none"

        pg.fill("#op-lp-input", "selectable draft")
        assert pg.locator("#op-lp-input").evaluate(
            "el => getComputedStyle(el).userSelect") == "text"
        pg.locator("#op-lp-input").select_text()
        assert pg.locator("#op-lp-input").evaluate(
            "el => el.selectionEnd-el.selectionStart") == len("selectable draft")
    finally:
        ctx.close()


def test_launchpad_composer_grows_and_shrinks_for_multiline_drafts(browser, harness):
    """Splash drafts expose wrapped/newline rows, then return to pill height."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.bring_to_front()
        pg.wait_for_selector("#op-lp", state="visible", timeout=8000)
        pg.wait_for_function(
            "document.getElementById('op-lp-input')._wired === true",
            timeout=8000, polling=50)
        baseline = pg.evaluate("""() => ({
          input:document.getElementById('op-lp-input').getBoundingClientRect().height,
          composer:document.querySelector('.op-lp-composer').getBoundingClientRect().height})""")

        # One TYPED line is the row unit. The EMPTY box is deliberately taller
        # than a typed line — the splash grows its own font while the
        # placeholder shows, and the 1.3em clamp grows with it — so the empty
        # baseline understates the rows and would fake-fail the growth assert.
        pg.fill("#op-lp-input", "one")
        pg.wait_for_timeout(80)
        row = pg.evaluate("() => document.getElementById('op-lp-input')"
                          ".getBoundingClientRect().height")

        pg.fill("#op-lp-input", "one\ntwo\nthree\nfour\nfive")
        pg.wait_for_function(
            "document.getElementById('op-lp-input').getBoundingClientRect().height > 60",
            timeout=3000, polling=50)
        expanded = pg.evaluate("""() => {
          const input=document.getElementById('op-lp-input');
          const composer=document.querySelector('.op-lp-composer').getBoundingClientRect();
          const send=document.getElementById('op-lp-send').getBoundingClientRect();
          return {input:input.getBoundingClientRect().height, composer:composer.height,
            client:input.clientHeight, scroll:input.scrollHeight,
            sendBottom:composer.bottom-send.bottom};
        }""")
        assert expanded["input"] >= row * 4.5
        assert expanded["composer"] >= baseline["composer"] + 40
        assert expanded["scroll"] <= expanded["client"] + 1
        assert 4 <= expanded["sendBottom"] <= 7

        pg.fill("#op-lp-input", "wrapped text " * 45)
        pg.wait_for_function(
            "document.getElementById('op-lp-input').getBoundingClientRect().height > 40",
            timeout=3000, polling=50)
        wrapped_height = pg.locator("#op-lp-input").evaluate(
            "el => el.getBoundingClientRect().height")
        assert wrapped_height > baseline["input"] * 2

        pg.fill("#op-lp-input", "short")
        pg.wait_for_function(
            "document.getElementById('op-lp-input').getBoundingClientRect().height < 30",
            timeout=3000, polling=50)
        shrunk = pg.evaluate("""() => ({
          input:document.getElementById('op-lp-input').getBoundingClientRect().height,
          composer:document.querySelector('.op-lp-composer').getBoundingClientRect().height})""")
        assert shrunk["input"] <= baseline["input"] + 1
        assert shrunk["composer"] <= baseline["composer"] + 1

        pg.fill("#op-lp-input", "first")
        pg.press("#op-lp-input", "Shift+Enter")
        pg.type("#op-lp-input", "second")
        assert pg.locator("#op-lp-input").input_value() == "first\nsecond"
    finally:
        ctx.close()


def test_chat_composer_expands_and_shrinks_for_multiline_drafts(browser, harness):
    """The rail composer fits a useful multiline draft before it starts scrolling."""
    # The rail composer belongs to a conversation. A blank initial session
    # remains on the launchpad and does not yet own an editing lease.
    ctx = _restored_ctx(browser, viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=8000)
        pg.wait_for_function("!document.getElementById('op-input').disabled", timeout=8000)
        pg.fill("#op-input", "one line")
        pg.wait_for_timeout(80)
        baseline = pg.locator("#op-input").bounding_box()["height"]
        pg.fill("#op-input", "one\ntwo\nthree\nfour\nfive\nsix\nseven")
        pg.wait_for_timeout(80)
        grown = pg.locator("#op-input").evaluate(
            "el => ({height: el.getBoundingClientRect().height, "
            "client: el.clientHeight, scroll: el.scrollHeight})")
        assert grown["height"] >= 105
        assert grown["scroll"] <= grown["client"] + 1

        pg.fill("#op-input", "one line")
        pg.wait_for_timeout(80)
        shrunk = pg.locator("#op-input").bounding_box()["height"]
        assert shrunk <= baseline + 1
    finally:
        ctx.close()


def test_saved_pill_is_permanent_with_a_minimal_empty_state(browser, harness):
    """Saved is a PERMANENT category (the owner 2026-07-19, superseding the
    appears-after-first-save contract): an empty account keeps the pill and
    its view reads "No saved tasks"; the first save fills it in place."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    tasks = []

    def task_api(route):
        if route.request.method == "POST":
            body = route.request.post_data_json
            tasks.append({"slug": "first-task", "name": body["name"],
                          "prompt": body["task"], "sites": [], "bot": "",
                          "model": "", "effort": "", "vars": []})
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "slug": "first-task"}))
            return
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "tasks": tasks}))

    pg.route("**/operator/tasks", task_api)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp-input", state="visible", timeout=8000)
        pg.wait_for_selector("#op-lp-tasks-toggle", state="visible", timeout=8000)
        pg.wait_for_function(
            "document.getElementById('op-lp-tasks-toggle')._wired === true",
            timeout=8000, polling=50)

        # empty Saved view: pill activates, grid is empty, minimal empty state
        pg.dispatch_event("#op-lp-tasks-toggle", "click")
        pg.wait_for_timeout(400)
        assert pg.locator("#op-lp-tasks-toggle").get_attribute("aria-pressed") == "true"
        assert pg.locator("#op-lp-title").text_content() == "Saved tasks"
        assert pg.locator(".op-lp-card").count() == 0
        assert pg.locator("#op-lp-empty").is_visible()
        assert pg.locator("#op-lp-empty").text_content() == "No saved tasks"

        pg.dispatch_event("#op-lp-add", "click")
        pg.fill("#op-nt-name", "Morning brief")
        pg.fill("#op-nt-prompt", "Summarize the morning news")
        pg.dispatch_event("#op-nt-save", "click")

        # the pill never left; the saved view fills in place
        pg.wait_for_selector(".op-lp-card", state="visible", timeout=3000)
        assert pg.locator("#op-lp-tasks-toggle").is_visible()
        assert pg.locator("#op-lp-empty").is_hidden()
    finally:
        ctx.close()


def test_rejected_saved_task_keeps_idle_state_and_never_executes_metadata(browser, harness):
    """Persisted task metadata must never become status-card markup."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    hostile_bot = (
        '</span><img id="saved-task-xss" src="missing" '
        'onerror="window.__savedTaskXss = true">'
    )

    def task_api(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "tasks": [{
                "slug": "hostile-task",
                "name": "Hostile metadata",
                "prompt": "Check the page",
                "sites": [],
                "bot": hostile_bot,
                "model": "",
                "effort": "",
                "vars": [],
            }]}),
        )

    pg.route("**/operator/tasks", task_api)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op-lp-tasks-toggle')._wired === true",
            timeout=8000,
            polling=50,
        )
        # The launchpad wires before driver discovery installs the task runner.
        # Clicking earlier only loads the prompt, so no status card is rendered.
        pg.wait_for_function("typeof window._opRunSavedTask === 'function'")
        pg.dispatch_event("#op-lp-tasks-toggle", "click")
        pg.wait_for_selector(".op-lp-card", state="visible", timeout=3000)
        pg.wait_for_function("typeof window._opRunSavedTask === 'function'")
        with pg.expect_response(lambda r: r.url.endswith('/hostile-task/run')) as response:
            pg.dispatch_event(".op-lp-card .op-lp-go", "click")
        assert response.value.status == 403
        pg.wait_for_function("document.getElementById('op-action-sub').textContent.includes('idle')")
        assert pg.evaluate("window.__savedTaskXss === true") is False
        assert pg.locator("#saved-task-xss").count() == 0
        assert hostile_bot not in pg.locator("#op-action-sub").text_content()
        assert harness.run_posts == ['/operator/tasks/hostile-task/run']
    finally:
        ctx.close()


@pytest.mark.parametrize('foreign', [False, True])
def test_saved_task_real_route_enforces_origin_and_dispatches_valid_bundle(browser, harness, monkeypatch, foreign):
    store = harness.mod.operator_tasks_store
    slug, error = store.save_task({'name': 'Fixture task', 'prompt': 'Check fixture', 'bot': 'gpt'})
    assert error is None
    harness.allowed_task_slug = slug
    starts = []
    monkeypatch.setattr(harness.mod._streamer, 'require_ready', lambda: None)
    # Replace only the consequential process launch, leaving the HTTP guard,
    # saved bundle lookup, dispatch assembly and last_run update real.
    def start(bot, prompt, **kwargs):
        starts.append((bot, prompt, kwargs))
        return {'ok': True}
    monkeypatch.setattr(harness.mod.operator_agent.runner, 'start', start)
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        headers = {'Origin': 'https://foreign.invalid'} if foreign else {}
        if foreign:
            response = ctx.request.post(harness.base + f'/operator/tasks/{slug}/run',
                data={'conversation_id': 'fixture-conversation'}, headers=headers)
            assert response.status == 403
        else:
            page.wait_for_function("typeof window._opRunSavedTask === 'function'")
            with page.expect_response(lambda r: r.url.endswith(f'/{slug}/run')) as response:
                page.evaluate('task => window._opRunSavedTask(task)', {**store.get_task(slug), 'slug': slug})
            assert response.value.status == 200
        assert len(starts) == (0 if foreign else 1)
        assert bool(store.get_task(slug)['last_run']) is (not foreign)
        if starts:
            assert starts[0][:2] == ('gpt', 'Check fixture')
            assert starts[0][2]['conversation_id']
    finally:
        ctx.close()


def test_invalid_saved_bot_is_rejected_by_real_runner_without_markup(browser, harness, monkeypatch):
    hostile = '</span><img id="saved-task-xss" src="missing" onerror="window.__savedTaskXss=true">'
    store = harness.mod.operator_tasks_store
    slug, error = store.save_task({'name': 'Invalid bot fixture', 'prompt': 'Fixture', 'bot': hostile})
    assert error is None
    harness.allowed_task_slug = slug
    monkeypatch.setattr(harness.mod._streamer, 'require_ready', lambda: None)
    # The real runner rejects before allocating a process or opening a job.
    monkeypatch.setattr(harness.mod.operator_agent.subprocess, 'Popen',
        lambda *a, **kw: pytest.fail('invalid metadata reached process launch'))
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function("typeof window._opRunSavedTask === 'function'")
        with page.expect_response(lambda r: r.url.endswith(f'/{slug}/run')) as response:
            page.evaluate('task => window._opRunSavedTask(task)', {**store.get_task(slug), 'slug': slug})
        assert response.value.status == 409
        assert not store.get_task(slug)['last_run']
        page.wait_for_function("document.getElementById('op').dataset.busy === '0'")
        assert page.locator('#saved-task-xss').count() == 0
        assert not page.evaluate('window.__savedTaskXss === true')
    finally:
        ctx.close()


def test_mobile_launchpad_uses_the_full_screen(browser, harness):
    """The mobile splash replaces the bottom sheet instead of sitting behind it."""
    # The harness' deliberately tiny _base.html omits the production viewport
    # meta tag, so use a narrow desktop context to exercise the same CSS query.
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp-wordmark", state="visible", timeout=8000)
        assert pg.locator(".op-rail").evaluate(
            "el => getComputedStyle(el).display") == "none"
        stage = pg.locator("#op-stage").bounding_box()
        assert stage is not None
        assert stage["y"] + stage["height"] >= 840

        pg.fill("#op-lp-input", "Find a nearby coffee shop")
        pg.press("#op-lp-input", "Enter")
        pg.wait_for_timeout(700)
        assert pg.locator(".op-rail").evaluate(
            "el => getComputedStyle(el).display") != "none"
    finally:
        ctx.close()


def test_history_run_again_redispatches_row_bundle(browser, harness):
    """1.0.13: ↻ on a History row re-dispatches with the ROW's bot/model/
    effort/surface — not the current pickers."""
    import time as _t
    import types
    import operator_history as OH
    rid = OH.record(types.SimpleNamespace(
        bot="gpt", task="scan the weekly filings", state="done",
        model="gpt-6.1-sol", effort="low", surface="browser", demo=False,
        started_ts=_t.time() - 120, ended_ts=_t.time() - 60,
        _runtime="codex", _cumulative_in_tokens=1000, _peak_in_tokens=500,
        messages=[{"ts": _t.time() - 90, "role": "assistant",
                   "text": "found the filings summary"}]), reason="exit 0")
    assert rid is not None
    harness.dispatch_posts.clear()
    ctx = browser.new_context()
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_timeout(800)
        pg.evaluate("document.getElementById('op-ham-history').click()")
        pg.wait_for_selector(".op-hist-rerun", timeout=8000)
        # 1.0.15: row click expands the inline trace (lazy-fetched detail) —
        # wait past the transient 'loading…' placeholder for the fetch to land
        pg.dispatch_event(".op-hist-row .task", "click")
        pg.wait_for_function(
            "() => { const t = document.querySelector('.op-hist-trace');"
            " return t && t.textContent && !t.textContent.includes('loading'); }",
            timeout=8000, polling=50)
        assert "found the filings summary" in \
            pg.locator(".op-hist-trace").text_content()
        pg.dispatch_event(".op-hist-row .task", "click")     # toggle closed again
        pg.wait_for_timeout(300)
        assert pg.locator(".op-hist-trace").count() == 0
        pg.dispatch_event(".op-hist-rerun", "click")
        pg.wait_for_timeout(800)
        assert len(harness.dispatch_posts) == 1
        body = harness.dispatch_posts[0]
        assert body["bot"] == "gpt"
        assert body["task"] == "scan the weekly filings"
        assert body["model"] == "gpt-6.1-sol"
        assert body["effort"] == "low"
        assert body["surface"] == "browser"
        assert errors == [], f"JS errors: {errors}"
    finally:
        ctx.close()


def test_restored_session_boot_completes_launchpad_init(browser, harness):
    """Boot with a cached nonempty auto-mode log: the splash must yield to the
    restored cockpit (not sit painted-but-dead over it) and the initializer
    must run to its final step — the saved-tasks fetch. Production evidence of
    the bug: /operator/models completed, /operator/tasks never requested."""
    ctx = _restored_ctx(browser, viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    errors, con_errors, tasks_reqs = _collectors(pg)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        # restored conversation on screen…
        pg.wait_for_selector("#op-log .op-msg", state="attached", timeout=8000)
        # …and the splash steps aside instead of lying dead on top of it
        pg.wait_for_selector("#op-lp", state="hidden", timeout=4000)
        # a COMPLETED init always ends in the saved-tasks hydration fetch
        pg.wait_for_timeout(600)
        assert tasks_reqs, "initLaunchpad never reached refreshLaunchpadTasks"
        assert errors == [], f"JS errors: {errors}"
        assert con_errors == [], f"console errors: {con_errors}"
    finally:
        ctx.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_launchpad_connection_ring_tracks_demand_start(browser, harness, width):
    """A down CDP probe while Chrome starts is yellow, not a red failure.

    Real page/CSS, synthetic status and dead CDP: no Chrome or model work.
    """
    harness.mode = 'live'
    status = dict(_STATUS_DEAD, status='connecting', browser_up=False,
                  detail='starting browser…')
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    pg = ctx.new_page()
    pg.clock.install()
    errors = []
    pg.on('pageerror', lambda error: errors.append(str(error)))
    pg.route('**/operator/status', lambda route: route.fulfill(json=status))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.add_style_tag(content=':root { --bad: #f85149; --fg: #ffffff; }')
        pg.wait_for_selector('#op.op-browser-down[data-connection="connecting"]')
        pg.wait_for_function("!document.getElementById('op').classList.contains('op-booting')")
        pg.evaluate("""() => {
          const style = document.getElementById('op').style;
          const set = style.setProperty.bind(style);
          window.__headerWrites = 0;
          style.setProperty = (name, ...args) => {
            if (name === '--op-hdr-h') window.__headerWrites++;
            return set(name, ...args);
          };
        }""")
        # Cross both the old 4s branding cutoff and the 6s cold-feed overlay.
        pg.clock.run_for(6600)

        def appearance():
            return pg.evaluate("""() => ({
              ring: getComputedStyle(document.querySelector('.op-lp-mark-sweep')).stroke,
              glyph: getComputedStyle(document.querySelector('.op-lp-mark-glyph')).stroke,
              animation: getComputedStyle(document.querySelector('.op-lp-mark-glyph')).animationName,
              copy: getComputedStyle(document.querySelector('.op-lp-mark-tip'), '::before').content,
              label: document.getElementById('op-lp-mark').getAttribute('aria-label')
            })""")

        # An intentionally hidden header must not cause per-frame style writes.
        writes = pg.evaluate('window.__headerWrites')
        assert pg.locator('#op').evaluate("el => el.style.getPropertyValue('--op-hdr-h')") == '0px'
        assert writes <= 20, f'idle header measurement kept retrying: {writes}'
        pending = appearance()
        assert pending['ring'] == 'rgb(231, 185, 62)'
        assert pending['glyph'] == 'rgb(245, 245, 245)'
        assert pending['animation'] == 'op-mark-glyph-turn'
        assert pending['copy'] == '"Starting browser…"'
        assert pending['label'] == 'Starting browser…'
        pg.emulate_media(reduced_motion='reduce')
        assert appearance()['animation'] == 'none'
        pg.emulate_media(reduced_motion='no-preference')

        # A reachable control port without an attached frame is still pending.
        status.update(browser_up=True, status='connecting', detail='attaching')
        pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        pg.wait_for_selector('#op:not(.op-browser-down)[data-connection="connecting"]')
        assert appearance()['label'] == 'Connecting to browser…'
        assert pg.locator('#op').get_attribute('data-state') != 'live'

        # Backend reports the actual launcher failure/timeout.
        status.update(status='error', detail='browser could not start: timed out')
        pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        pg.wait_for_selector('#op[data-connection="error"]')
        pg.wait_for_function("getComputedStyle(document.querySelector('.op-lp-mark-sweep')).stroke === 'rgb(248, 81, 73)'")
        assert appearance()['ring'] == 'rgb(248, 81, 73)'
        assert appearance()['copy'] == '"Connection error"'

        # A retry must spin again even though the boot animation already settled.
        status.update(status='connecting')
        pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        pg.wait_for_selector('#op[data-connection="connecting"]')
        assert appearance()['animation'] == 'op-mark-glyph-turn'

        # An attached frame outranks the briefly cached negative CDP probe.
        status.update(_STATUS_LIVE)
        pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        pg.wait_for_selector('#op[data-connection="live"]')
        pg.wait_for_function("getComputedStyle(document.querySelector('.op-lp-mark-sweep')).stroke === 'rgb(63, 185, 80)'")
        assert appearance()['ring'] == 'rgb(63, 185, 80)'
        assert appearance()['copy'] == '"Connected"'
        assert appearance()['animation'] != 'op-mark-glyph-turn'
        # A lost frame clears green immediately, even while CDP stays up.
        status.update(has_frame=False, status='connecting', browser_up=True)
        pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        pg.wait_for_selector('#op[data-connection="connecting"][data-state="connecting"]')
        # An explicit failure also outranks a briefly cached frame.
        status.update(has_frame=True, status='error', detail='attachment failed')
        pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        pg.wait_for_selector('#op[data-connection="error"][data-state="error"]')
        assert appearance()['label'] == 'Connection error'
        if width == 390:
            # Header-only layout changes must follow even without window.resize.
            pg.add_style_tag(content='#test-site-header { display:block!important; height:31px; padding:0; border:0; box-sizing:border-box; }')
            pg.wait_for_function("document.getElementById('op').style.getPropertyValue('--op-hdr-h') === '31px'")
            pg.locator('#test-site-header').evaluate("el => el.style.height = '43px'")
            pg.wait_for_function("document.getElementById('op').style.getPropertyValue('--op-hdr-h') === '43px'")
        assert not errors, errors
    finally:
        ctx.close()


def test_launchpad_connection_colours_interpolate_between_states(browser, harness):
    """Sample the rendered midpoint of real CSS transitions in both directions."""
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 1440, 'height': 900})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    pg = ctx.new_page()
    pg.route('**/operator/status', lambda route: route.fulfill(json=_STATUS_LIVE))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.wait_for_selector('#op[data-connection="live"]')
        pg.wait_for_selector('.op-lp-mark', state='visible')
        pg.add_style_tag(content=':root { --bad: #f85149; }')
        samples = pg.evaluate("""async () => {
          const root = document.getElementById('op');
          const ring = root.querySelector('.op-lp-mark-sweep');
          const colour = () => getComputedStyle(ring).stroke;
          const paint = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          const samples = [];
          for (const state of ['connecting', 'error', 'live']) {
            for (const animation of ring.getAnimations()) animation.finish();
            await paint();
            const from = colour();
            root.dataset.connection = state;
            colour(); // Resolve the changed style and instantiate its transition.
            const transition = ring.getAnimations().find(a => a.transitionProperty === 'stroke');
            if (!transition) throw new Error('Missing stroke transition to ' + state);
            transition.pause();
            const duration = transition.effect.getComputedTiming().duration;
            transition.currentTime = duration / 2;
            const middle = colour();
            transition.finish();
            samples.push({from, middle, to: colour(), duration});
          }
          return samples;
        }""")
        for sample in samples:
            assert sample['middle'] not in (sample['from'], sample['to'])
            assert 200 <= sample['duration'] <= 600
    finally:
        ctx.close()


def test_restored_session_home_reopens_live_launchpad(browser, harness):
    """After a restored conversation, HOME must reopen the splash with every
    control live: cards populate the splash composer, category pills toggle
    aria-pressed and swap the grid, X dismisses, and HOME works AGAIN after
    that dismissal (the controls stay wired across show/hide cycles)."""
    ctx = _restored_ctx(browser, viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    errors, con_errors, _ = _collectors(pg)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=4000)

        # HOME reopens the solid splash mid-conversation (v1.0.21 seed of the
        # sessions sidebar) — auto mode keeps #op-lp-open visible
        pg.wait_for_selector("#op-lp-open", state="visible", timeout=4000)
        pg.dispatch_event("#op-lp-open", "click")
        pg.wait_for_selector("#op-lp", state="visible", timeout=2000)
        pg.wait_for_timeout(150)
        assert pg.locator(".op-lp-card").count() > 0, "no cards rendered"

        # a card tap drafts into the splash composer (never auto-fires)
        pg.locator(".op-lp-card").first.dispatch_event("click")
        assert pg.locator("#op-lp-input").input_value(), "card tap drew blank"

        # a category pill takes the highlight and swaps the grid
        pg.dispatch_event('.op-lp-cat[data-category="media"]', "click")
        assert pg.locator('.op-lp-cat[data-category="media"]').get_attribute(
            "aria-pressed") == "true"
        pg.wait_for_timeout(300)   # grid cross-fade
        assert pg.locator(".op-lp-card").count() > 0

        # X dismisses; the restored chat is still there underneath
        pg.dispatch_event("#op-lp-x", "click")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=2000)
        assert pg.locator("#op-log .op-msg").count() >= 2

        # …and HOME still works after the dismissal — wiring survives cycles
        pg.dispatch_event("#op-lp-open", "click")
        pg.wait_for_selector("#op-lp", state="visible", timeout=2000)
        pg.dispatch_event('.op-lp-cat[data-category="travel"]', "click")
        assert pg.locator('.op-lp-cat[data-category="travel"]').get_attribute(
            "aria-pressed") == "true"
        assert errors == [], f"JS errors: {errors}"
        assert con_errors == [], f"console errors: {con_errors}"
    finally:
        ctx.close()


def test_launchpad_controls_work_while_tasks_fetch_is_stalled(browser, harness):
    """The saved-tasks endpoint hanging must not take the local examples or
    any splash control with it (companion to the stalled-models contract)."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    stalled = []
    pg.route("**/operator/tasks", lambda route: stalled.append(route))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="visible", timeout=8000)
        pg.wait_for_timeout(200)
        assert stalled, "tasks fetch never left the gate"

        # examples are local data — they must paint and stay interactive
        _expand_launchpad(pg)
        assert pg.locator(".op-lp-card").count() > 0
        pg.locator(".op-lp-card").first.dispatch_event("click")
        assert pg.locator("#op-lp-input").input_value()

        pg.dispatch_event('.op-lp-cat[data-category="research"]', "click")
        assert pg.locator('.op-lp-cat[data-category="research"]').get_attribute(
            "aria-pressed") == "true"

        pg.dispatch_event("#op-lp-x", "click")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=2000)
    finally:
        ctx.close()


def test_restored_session_touch_activation(browser, harness):
    """Touch-media pass over the restored-session controls.

    The context exercises coarse/touch CSS. DOM click dispatch checks the
    control handlers because headless Chromium's compositor can indefinitely
    stall Playwright's tap transport; real-iPad touch remains a release gate.
    """
    ctx = _restored_ctx(browser, has_touch=True,
                        viewport={"width": 1024, "height": 1366})
    pg = ctx.new_page()
    errors, con_errors, _ = _collectors(pg)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=4000)

        pg.dispatch_event("#op-lp-open", "click")
        pg.wait_for_selector("#op-lp", state="visible", timeout=2000)
        pg.wait_for_timeout(150)

        pg.locator(".op-lp-card").first.dispatch_event("click")
        assert pg.locator("#op-lp-input").input_value(), "tap drew blank draft"
        # a second card swaps the draft, never stacks or auto-fires
        pg.locator(".op-lp-card").nth(1).dispatch_event("click")
        assert pg.locator("#op-lp-input").input_value()
        assert pg.locator("#op-log .op-msg").count() >= 2   # no dispatch fired

        pg.dispatch_event('.op-lp-cat[data-category="shopping"]', "click")
        assert pg.locator('.op-lp-cat[data-category="shopping"]').get_attribute(
            "aria-pressed") == "true"

        pg.dispatch_event("#op-lp-x", "click")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=2000)
        assert errors == [], f"JS errors: {errors}"
        assert con_errors == [], f"console errors: {con_errors}"
    finally:
        ctx.close()


@pytest.mark.parametrize('delete_status', [200, 409, 503])
def test_header_delete_stays_deleted_after_splash_and_reload(browser, harness, monkeypatch, delete_status):
    import operator_workspace
    monkeypatch.setattr(operator_workspace, 'delete_conversation', lambda sid: None)
    monkeypatch.setattr(harness.mod, '_browser_tab_command', lambda *a, **kw: True)
    harness.mode = 'live'
    sid = OS_MOD.create()['id']
    OS_MOD.save(dict(_SEEDED_SESSION), conversation_id=sid)
    ctx = browser.new_context(viewport={'width': 1440, 'height': 900})
    ctx.add_init_script("if (!sessionStorage.getItem('operator-conversation-v2')) "
                        "sessionStorage.setItem('operator-conversation-v2', " + json.dumps(sid) + ");")
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda e: errors.append(str(e)))
    pg.route('**/operator/agent?*', lambda r: r.fulfill(json={'state': 'idle', 'alive': False, 'messages': []}))
    if delete_status != 200:
        pg.route('**/operator/sessions/*?fresh=1', lambda r: r.fulfill(
            status=delete_status, json={'ok': False, 'error': 'Deletion unavailable'}))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.wait_for_selector('#op-lp', state='hidden', timeout=8000)
        if delete_status != 200:
            dialogs = []
            pg.on('dialog', lambda d: (dialogs.append(d.message), d.accept()))
            pg.locator('#op-clear').click()
            pg.wait_for_function("!document.getElementById('op-clear').disabled")
            assert dialogs == ['Deletion unavailable']
            assert pg.locator('#op-log .op-msg').count() > 0
            assert OS_MOD.load(sid)['data']['log']
            return
        with pg.expect_navigation(wait_until='domcontentloaded'):
            pg.locator('#op-clear').click()
        pg.wait_for_function("!document.getElementById('op').classList.contains('op-booting')")
        assert pg.locator('#op-log').inner_text() == '', pg.locator('#op-log').inner_html()
        pg.wait_for_selector('#op-lp', state='visible', timeout=8000)
        replacement = pg.evaluate("sessionStorage.getItem('operator-conversation-v2')")
        assert replacement != sid
        with pytest.raises(KeyError):
            OS_MOD.load(sid)
        # Let remote-session synchronization complete before checking that
        # leaving the splash cannot restore the deleted transcript.
        with pg.expect_response(lambda response:
                '/operator/session?' in response.url
                and response.request.method == 'GET'):
            pg.dispatch_event('#op-lp-x', 'click')
        pg.evaluate('() => new Promise(requestAnimationFrame)')
        assert pg.locator('#op-log .op-msg').count() == 0
        pg.reload(wait_until='domcontentloaded')
        pg.wait_for_selector('#op-lp', state='visible', timeout=8000)
        assert pg.locator('#op-log .op-msg').count() == 0
        assert not errors
    finally:
        ctx.close()


def test_trash_clear_returns_to_opaque_splash(browser, harness):
    """Trashing a conversation lands on the SOLID splash, not the translucent
    over-the-feed blur (the owner 2026-07-18, superseding the 07-17 blur note).

    Since 1.1.0 the trash button deletes the durable conversation and
    RELOADS into the fresh one, so the splash arrives after a full page boot:
    wait on that navigation, not on a timer sized for the old in-place clear."""
    ctx = _restored_ctx(browser, viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    errors, con_errors, _ = _collectors(pg)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=4000)
        pg.dispatch_event("#op-clear", "click")
        pg.wait_for_function(_RELOADED, timeout=20000)        # the reload landed
        pg.wait_for_selector("#op-lp", state="visible", timeout=8000)
        assert not pg.eval_on_selector(
            "#op-lp", "el => el.classList.contains('op-lp-over')"), \
            "trash must present the opaque splash, not the blur overlay"
        # the splash it lands on is live: a card drafts into the composer
        pg.wait_for_timeout(150)
        _expand_launchpad(pg)
        pg.locator(".op-lp-card").first.dispatch_event("click")
        assert pg.locator("#op-lp-input").input_value()
        assert errors == [] and con_errors == []
    finally:
        ctx.close()


def test_splash_composer_ios_scaled_geometry(browser, harness):
    """The coarse-pointer WebKit composer: computed 16px painted at 0.7x, in a
    BLOCK pill with overflow clipping. Chromium can't match the @supports
    WebKit gate, so the shipped declarations are injected verbatim and the
    geometry that killed the 1.0.23 hack is held here:
      * no flex defeat — the widened layout box sticks, text paints edge to
        edge of the pill's inner width (not squished to ~70%),
      * per-line pill growth — the negative-margin trim subtracts cleanly in
        block flow (the centered-flex version grew +0.2px for 2 lines),
      * containment — the input's painted box stays inside the clipping pill,
        so text and caret cannot escape the rounded bounds,
      * chat-style cap — grows to ~9 painted lines, then scrolls internally,
        and shrinks back to the one-line pill."""
    # every declaration !important: the injected <style> precedes the page's
    # body-level <link> in tree order, while the real @supports block wins by
    # coming later in the same sheet — importance stands in for position.
    IOS_DECLS = (
        ".op-lp-composer { display: block !important; overflow: hidden !important;"
        " border-radius: 22px !important; min-height: 0 !important;"
        " padding: 0.86rem 3rem 0.86rem 0.92rem !important; }"
        " .op-lp-input { font-size: 16px !important; width: 142.857% !important;"
        " transform: scale(.7) !important; transform-origin: left top !important; }"
        " .op-lp-input:placeholder-shown { margin-bottom: -0.39em !important; }"
        " .op-lp-input::placeholder { color: transparent !important; opacity: 0 !important; }"
        " .op-lp-placeholder { position: absolute !important;"
        " inset: 0 3rem 0 0.92rem !important; align-items: center !important;"
        " pointer-events: none !important;"
        " font: 500 calc(0.672rem * var(--chat-scale))/1.14 'DM Sans', var(--chat-font) !important;"
        " letter-spacing: -.01em !important; white-space: nowrap !important; }"
        " .op-lp-composer:has(.op-lp-input:placeholder-shown) .op-lp-placeholder"
        " { display: flex !important; }"
        " .op-lp-composer:not(:has(.op-lp-input:placeholder-shown)) .op-lp-placeholder"
        " { display: none !important; }")
    ctx = browser.new_context(viewport={"width": 1024, "height": 1366})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="visible", timeout=8000)
        pg.add_style_tag(content=IOS_DECLS)
        pg.evaluate("document.getElementById('op-lp-input')"
                    ".dispatchEvent(new Event('input'))")   # re-measure post-inject
        pg.wait_for_timeout(150)

        def geo():
            return pg.evaluate("""() => {
              const i = document.getElementById('op-lp-input');
              const c = document.querySelector('.op-lp-composer');
              const ir = i.getBoundingClientRect(), cr = c.getBoundingClientRect();
              const cs = getComputedStyle(c);
              return {iw: ir.width, ih: ir.height, cw: cr.width, ch: cr.height,
                      inside: ir.left >= cr.left - 1 && ir.right <= cr.right + 1
                           && ir.top >= cr.top - 1 && ir.bottom <= cr.bottom + 1,
                      clip: cs.overflow === 'hidden',
                      scroll: i.scrollHeight, client: i.clientHeight};
            }""")

        base = geo()
        # painted line is the compact 11.2px face: one line ≈ 16*1.3*0.7
        assert 13 <= base["ih"] <= 17, f"one painted line expected: {base['ih']}"
        # no flex defeat: painted text spans the pill inner width (pill minus
        # the 3rem send gutter and 0.92rem left pad, ±10px slack)
        assert base["iw"] >= base["cw"] - 75, f"squished input: {base}"
        assert base["clip"], "composer must clip (caret containment)"
        placeholder_delta = pg.evaluate("""() => {
          const p = document.querySelector('.op-lp-placeholder');
          const c = document.querySelector('.op-lp-composer');
          const range = document.createRange();
          range.selectNodeContents(p);
          const pr = range.getBoundingClientRect(), cr = c.getBoundingClientRect();
          return (pr.top + pr.height / 2) - (cr.top + cr.height / 2);
        }""")
        assert abs(placeholder_delta) <= 0.75, \
            f"placeholder off pill center by {placeholder_delta:.2f}px"

        pg.fill("#op-lp-input", "wrapped splash draft " * 15)
        pg.wait_for_timeout(150)
        grown = geo()
        # pill stretches by at least two painted lines and keeps every line
        # on screen (no internal scroll yet), input stays inside the pill
        assert grown["ch"] >= base["ch"] + 24, \
            f"pill did not stretch: {base['ch']} -> {grown['ch']}"
        assert grown["scroll"] <= grown["client"] + 1
        assert grown["inside"], f"input escaped the pill: {grown}"

        pg.fill("#op-lp-input", "long draft line " * 120)   # far past the cap
        pg.wait_for_timeout(150)
        capped = geo()
        # chat-style ceiling: pill stops around the 140px visual cap
        # (+ padding) and the overflow scrolls internally
        assert capped["ch"] <= 185, f"pill blew past the cap: {capped['ch']}"
        assert capped["scroll"] > capped["client"] + 10, "no internal scroll at cap"
        assert capped["inside"]

        pg.fill("#op-lp-input", "short")
        pg.wait_for_timeout(150)
        shrunk = geo()
        assert shrunk["ch"] <= base["ch"] + 1, f"did not shrink: {shrunk['ch']}"
        assert pg.locator(".op-lp-placeholder").evaluate(
            "el => getComputedStyle(el).display") == "none"
    finally:
        ctx.close()


def test_chat_library_filters_sort_search_and_mobile_fit(browser, harness):
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    pg = ctx.new_page()
    rows = [
        {'id': 'a', 'title': 'Airfare', 'model': 'GPT-6 Astra', 'updated_ts': 1, 'presence': {'can_control': True, 'controller_label': 'HOST-B'}},
        {'id': 'b', 'title': 'Hotel', 'model': 'Gemini', 'updated_ts': 2, 'alive': True},
        {'id': 'c', 'title': 'Coffee', 'model': 'Sol', 'updated_ts': 3},
    ]
    pg.route('**/operator/sessions?*', lambda r: r.fulfill(json={'ok': True, 'sessions': rows}))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        # The stub host omits host-app's base stylesheet and color tokens.
        pg.add_style_tag(content=':root{--fg:#e7ecf3;--muted:#8e99a8;--border-2:#30343b} *{animation:none!important;transition:none!important}')
        pg.wait_for_function("document.getElementById('op-lp-open')._wired === true")
        pg.locator('#op-lp-open').dispatch_event('click')
        pg.wait_for_selector('#op-lp-chats', state='visible')
        pg.locator('#op-lp-chats').dispatch_event('click')
        pg.wait_for_function("document.getElementById('op-chat-count').textContent === '3 of 3 chats'")
        pg.locator('#op-chat-filter').select_option('running')
        assert pg.locator('.op-chat-title').all_text_contents() == ['Hotel']
        pg.locator('#op-chat-filter').select_option('here')
        assert pg.locator('.op-chat-title').all_text_contents() == ['Airfare']
        pg.locator('#op-chat-filter').select_option('all')
        pg.locator('#op-chat-sort').select_option('title')
        assert pg.locator('.op-chat-title').all_text_contents() == ['Airfare', 'Coffee', 'Hotel']
        pg.locator('#op-chat-search').fill('host-b')
        assert pg.locator('.op-chat-title').all_text_contents() == ['Airfare']
        pg.locator('#op-chat-search').fill('astra')
        assert pg.locator('.op-chat-title').all_text_contents() == ['Airfare']
        pg.locator('#op-chat-search').fill('')
        assert pg.locator('#op-chats').evaluate('el => el.scrollWidth <= el.clientWidth')
        pg.screenshot(path='/tmp/operator-chat-library-mobile.png')
    finally:
        ctx.close()


def test_category_search_checks_examples_beyond_visible_cards(browser, harness):
    """A category's six-card display cap must not truncate its search domain."""
    import re
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "static/js/operator.js").read_text()
    names = re.findall(r"name: '([^']+)'[^\n]+category: 'shopping'", source)
    assert len(names) > 6
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        _expand_launchpad(pg)
        pg.locator('.op-lp-cat[data-category="shopping"]').dispatch_event("click")
        pg.wait_for_timeout(400)
        visible = set(pg.locator('.op-lp-name').all_text_contents())
        target = next(name for name in names if name not in visible)
        pg.locator('#op-lp-search').dispatch_event('click')
        pg.locator('#op-lp-searchinput').fill(target)
        pg.wait_for_function("target => [...document.querySelectorAll('.op-lp-name')]"
                             ".some(el => el.textContent === target)", arg=target, timeout=3000)
    finally:
        ctx.close()


def test_chat_library_hides_delegations_until_asked(browser, harness):
    """Bot-made delegation chats (origin mcp) stay out of the human's library
    by default; the Delegations filter shows exactly them (2026-09-22)."""
    mine = OS_MOD.create()["id"]
    OS_MOD.save({"log": '<div class="op-msg user"><span class="bubble">mine</span></div>',
                 "preview": "Book the ferry", "bot": "gpt", "surface": "browser"},
                conversation_id=mine)
    OS_MOD.title_if_unset("Ferry", mine)
    bot = OS_MOD.create("Compare rates", activate=False, reuse_draft=False, origin="mcp")["id"]
    app = OS_MOD.create('App research', activate=False, reuse_draft=False, origin='app')['id']
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op-lp-open')._wired === true",
            timeout=8000, polling=50)
        pg.locator("#op-lp-open").dispatch_event("click")
        pg.wait_for_selector("#op-lp-chats", state="visible", timeout=8000)
        pg.locator("#op-lp-chats").dispatch_event("click")
        pg.wait_for_selector(".op-chat-row", state="visible", timeout=8000)
        assert pg.locator(".op-chat-title").all_text_contents() == ["Ferry"]
        pg.select_option("#op-chat-filter", "delegations")
        pg.wait_for_timeout(300)
        assert set(pg.locator('.op-chat-title').all_text_contents()) == {'Compare rates', 'App research'}
        pg.select_option("#op-chat-filter", "all")
        pg.wait_for_timeout(300)
        assert pg.locator(".op-chat-title").all_text_contents() == ["Ferry"]
        assert errors == []
    finally:
        ctx.close()
        for sid in (mine, bot, app):
            try:
                OS_MOD.delete(sid)
            except KeyError:
                pass


def test_chat_library_omits_the_empty_replacement_after_deletion(browser, harness):
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 1280, 'height': 800})
    pg = ctx.new_page()
    rows = [
        {'id': 'replacement', 'title': '', 'preview': '', 'empty': True},
        {'id': 'kept', 'title': 'Kept conversation', 'preview': 'Earlier task', 'empty': False},
    ]
    pg.route('**/operator/sessions?*', lambda r: r.fulfill(json={'ok': True, 'sessions': rows}))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.wait_for_function("document.getElementById('op-lp-open')._wired === true")
        pg.locator('#op-lp-open').dispatch_event('click')
        pg.locator('#op-lp-chats').dispatch_event('click')
        pg.wait_for_selector('#op-chat-list .op-chat-title')
        assert pg.locator('.op-chat-title').all_text_contents() == ['Kept conversation']
        assert pg.locator('#op-chat-count').inner_text() == '1 of 1 chats'
    finally:
        ctx.close()
