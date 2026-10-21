"""Streaming, browser input and session-control contracts for the real operator_view page.

Run through scripts/cc_test.py with this explicit file. Shared fixtures and
isolation live in cockpit_support. Shared dependencies are operator_view,
operator_workspace, operator.js, operator_feed.js, workbench.js, operator.css
and workbench.css; their boot and state contracts span these groups.
"""

import os
from pathlib import Path

from cockpit_support import (  # noqa: F401
    OV,
    _RELOADED,
    _SEEDED_SESSION,
    _STATUS_DEAD,
    _STATUS_LIVE,
    _connection_context,
    _fresh_session_store,
    _requested_tier,
    _restore_harness_env,
    _restored_ctx,
    _sample_signal_state,
    _seed_once_script,
    _stage_size,
    _transitions,
    _token_host_styles,
    browser,
    harness,
    json,
    page,
    pytest,
)


def test_hidden_cockpit_stops_status_polling_and_resumes(page, harness):
    harness.mode = "live"
    requests = []
    page.on("request", lambda req: requests.append(req.url)
            if req.url.endswith("/operator/status") else None)
    page.goto(harness.base + "/operator", wait_until="domcontentloaded")
    page.wait_for_timeout(1800)
    assert requests
    page.evaluate("""() => {
        window.__testHidden = true;
        Object.defineProperty(document, 'hidden', {configurable: true,
            get: () => window.__testHidden});
        Object.defineProperty(document, 'visibilityState', {configurable: true,
            get: () => window.__testHidden ? 'hidden' : 'visible'});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_timeout(500)
    hidden_count = len(requests)
    page.wait_for_timeout(2000)
    assert len(requests) == hidden_count
    page.evaluate("""() => {
        window.__testHidden = false;
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_timeout(500)
    assert len(requests) > hidden_count
    assert page._errors == []


def test_unwatched_cockpit_stops_frame_pulls_and_resumes_on_input(page, harness):
    harness.mode = "live"
    frames = []
    page.on("request", lambda req: frames.append(req.url)
            if "/operator/frame" in req.url else None)
    page.goto(harness.base + "/operator", wait_until="domcontentloaded")
    page.wait_for_timeout(1800)
    assert frames
    page.evaluate("""() => {
        window.OP_FEED_UNWATCHED_MS = 1500;
        document.hasFocus = () => false;
    }""")
    page.wait_for_timeout(3000)
    paused_count = len(frames)
    page.wait_for_timeout(2000)
    assert len(frames) == paused_count
    # Inside the window the pump wakes (every 350 ms) and pulls again.
    page.evaluate("() => window.dispatchEvent(new Event('pointerdown'))")
    page.wait_for_timeout(1200)
    assert len(frames) > paused_count
    assert page._errors == []


def test_boot_clean_fresh_session(page, harness):
    page.goto(harness.base + "/operator", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    assert page._errors == [], f"JS errors on fresh boot: {page._errors}"


@pytest.mark.parametrize("connection", [
    {"type": "cellular", "effectiveType": "4g"},
    {"type": "wifi", "effectiveType": "4g", "saveData": True},
    {"type": "wifi", "effectiveType": "3g"},
])
def test_feed_uses_eco_tier_for_metered_connection(browser, harness, connection):
    harness.mode = "live"
    ctx = _connection_context(browser, connection=connection)
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op').dataset.feedTier === 'eco'",
            timeout=5000, polling=50)
        assert _requested_tier(harness, "eco"), harness.frame_tiers
    finally:
        ctx.close()


def test_narrow_wifi_keeps_the_normal_mobile_tier(browser, harness):
    harness.mode = "live"
    ctx = _connection_context(browser, width=390)
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op').dataset.feedTier === 'lo'",
            timeout=5000, polling=50)
        assert _requested_tier(harness, "lo"), harness.frame_tiers
    finally:
        ctx.close()


def test_stream_quality_override_is_visible_persisted_and_beats_network(browser, harness):
    """The hamburger control is a device preference, not a suggestion.

    A user who pins High on cellular must get hi frames immediately and after
    reload; returning to Auto must expose the effective Low tier again.
    """
    harness.mode = "live"
    ctx = _connection_context(
        browser, width=390,
        connection={"type": "cellular", "effectiveType": "4g"},
    )
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps(_SEEDED_SESSION)) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op').dataset.feedTier === 'eco'",
            timeout=5000, polling=50)
        assert pg.locator("#op-ham-quality").input_value() == "auto"
        assert pg.locator("#op-ham-quality-effective").inner_text() == "Auto · Low"

        # Exercise the browser menu in its visible half-sheet state. Tall test
        # phones classify the initial fit detent as `full`, which deliberately
        # folds all browser chrome away.
        pg.evaluate("""() => {
          const op = document.getElementById('op');
          op.dataset.sheet = 'half'; op.style.setProperty('--sheet-h', '50dvh');
        }""")
        pg.wait_for_timeout(350)
        hit = pg.evaluate("""() => {
          const b = document.getElementById('op-ham-btn');
          const r = b.getBoundingClientRect();
          const el = document.elementFromPoint((r.left + r.right) / 2,
                                               (r.top + r.bottom) / 2);
          return {box: [r.left, r.top, r.right, r.bottom],
                  hit: el && (el.id || el.tagName)};
        }""")
        assert hit["hit"] in {"op-ham-btn", "svg", "path"}, hit
        pg.locator("#op-ham-btn").click()
        assert pg.locator("#op-ham-quality").is_visible()
        assert pg.evaluate("""() => {
          const s = document.getElementById('op-ham-quality');
          s.scrollIntoView({block: 'nearest'});
          const r = s.getBoundingClientRect();
          return document.elementFromPoint((r.left + r.right) / 2,
                                           (r.top + r.bottom) / 2) === s;
        }"""), "stream quality must be tappable above the mobile sheet"
        pg.locator("#op-ham-quality").select_option("high")
        pg.wait_for_function(
            "document.getElementById('op').dataset.feedTier === 'hi'",
            timeout=5000, polling=50)
        assert pg.locator("#op-ham-quality-effective").inner_text() == "High"
        assert pg.evaluate(
            "localStorage.getItem('operator-stream-quality-v1')") == "high"

        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op').dataset.feedTier === 'hi'",
            timeout=5000, polling=50)
        assert pg.locator("#op-ham-quality").input_value() == "high"

        pg.evaluate("""() => {
          const op = document.getElementById('op');
          op.dataset.sheet = 'half'; op.style.setProperty('--sheet-h', '50dvh');
        }""")
        pg.wait_for_timeout(350)
        pg.locator("#op-ham-btn").click()
        assert pg.locator("#op-ham-quality").is_visible()
        pg.locator("#op-ham-quality").select_option("auto")
        pg.wait_for_function(
            "document.getElementById('op').dataset.feedTier === 'eco'",
            timeout=5000, polling=50)
        assert pg.locator("#op-ham-quality-effective").inner_text() == "Auto · Low"
    finally:
        ctx.close()


@pytest.mark.parametrize("platform", ["Win32", "MacIntel"])
def test_hamburger_shortcuts_match_the_viewers_platform(browser, harness, platform):
    """Shortcut hints describe the viewer's keyboard; they are not decorative
    glyphs. Pin both platform branches independently of the test host."""
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    # This test asserts the non-Apple branch. The browser binary follows the
    # host platform, so declare the viewer platform instead of relying on a
    # Linux runner to supply it implicitly.
    ctx.add_init_script("""(() => {
      Object.defineProperty(navigator, 'userAgentData', {
        configurable: true, value: {platform: 'Linux'},
      });
      Object.defineProperty(navigator, 'platform', {
        configurable: true, value: 'Linux x86_64',
      });
    })()""")
    ctx.add_init_script("Object.defineProperty(navigator, 'userAgentData', {value: undefined});"
        + "Object.defineProperty(navigator, 'platform', {value: " + json.dumps(platform) + "});")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        hints = pg.evaluate("""() => Object.fromEntries(
          [...document.querySelectorAll('[data-shortcut]')]
            .map(el => [el.dataset.shortcut, el.textContent.trim()]))""")
        expected = {
            "reload": "Ctrl R",
            "hard-reload": "Ctrl Shift R",
            "zoom-in": "Ctrl +",
            "zoom-out": "Ctrl -",
            "zoom-reset": "Ctrl 0",
            "find": "Ctrl F",
            "select-all": "Ctrl A",
            "escape": "Esc",
            "next-tab": "Ctrl Tab",
        }
        if platform == 'MacIntel':
            expected = {key: value.replace('Ctrl Shift', '⇧ ⌘').replace('Ctrl', '⌘')
                        for key, value in expected.items()}
            expected['next-tab'] = '⌃ Tab'
        assert hints == expected
    finally:
        ctx.close()


@pytest.mark.parametrize(("preference", "tier"), [
    ("low", "eco"),
    ("medium", "lo"),
    ("high", "hi"),
])
def test_stream_quality_manual_levels_pin_each_tier(
        browser, harness, preference, tier):
    harness.mode = "live"
    ctx = _connection_context(browser, width=1280)
    ctx.add_init_script(
        f"localStorage.setItem('operator-stream-quality-v1', '{preference}')")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            f"document.getElementById('op').dataset.feedTier === '{tier}'",
            timeout=5000, polling=50)
        assert pg.locator("#op-ham-quality").input_value() == preference
        assert pg.locator("#op-ham-quality-effective").inner_text() == preference.title()
        assert _requested_tier(harness, tier), harness.frame_tiers
    finally:
        ctx.close()


@pytest.mark.parametrize(("legacy", "current"), [
    ("saver", "low"),
    ("sharp", "high"),
])
def test_stream_quality_migrates_legacy_saved_names(browser, harness, legacy, current):
    harness.mode = "live"
    ctx = _connection_context(browser, width=1280)
    ctx.add_init_script(
        f"localStorage.setItem('operator-stream-quality-v1', '{legacy}')")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            f"document.getElementById('op').dataset.feedQuality === '{current}'",
            timeout=5000, polling=50)
        assert pg.locator("#op-ham-quality").input_value() == current
        assert pg.evaluate(
            "localStorage.getItem('operator-stream-quality-v1')") == current
    finally:
        ctx.close()


def test_slow_frame_delivery_falls_back_to_eco_and_recovers(browser, harness):
    """Safari exposes no useful radio type. Three genuinely slow response-body
    transfers must still push the tab into eco; the decision is based on body
    throughput, not the server's long-poll wait."""
    harness.mode = "live"
    ctx = _connection_context(browser, connection={}, slow_body_ms=160)
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op').dataset.feedTier === 'eco'",
            timeout=8000, polling=50)
        pg.wait_for_timeout(500)  # allow the next pull to carry the new tier
        assert harness.frame_tiers.count("hi") >= 3
        assert "eco" in harness.frame_tiers
        pg.evaluate("window.__opSlowBodyMs = 0")
        pg.wait_for_function(
            "document.getElementById('op').dataset.feedTier === 'hi'",
            timeout=8000, polling=50)
        pg.wait_for_timeout(250)
        assert harness.frame_tiers[-1] == "hi"
    finally:
        ctx.close()


def test_boot_clean_seeded_session(browser, harness):
    # the 2026-06-26 TDZ crash only manifested WITH a restored session — seed
    # one at document start, before any page script runs.
    harness.mode = "real"
    ctx = browser.new_context()
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps(_SEEDED_SESSION)) + ");")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_timeout(3000)
        assert errors == [], f"JS errors on seeded boot: {errors}"
        # the restored log actually rendered (session restore ran)
        assert pg.locator("#op-log .op-msg").count() >= 2
        # dead-listener elements are stripped on restore
        assert pg.locator("#op-log .op-handoff").count() == 0
    finally:
        ctx.close()


def test_restored_squad_store_trace_uses_product_casing(browser, harness):
    harness.mode = "real"
    old_trace = ('<div class="op-task-step op-act-step">'
                 '<span class="op-act-ico2 op-connector-ico" '
                 'title="Using Squad Store">S</span>'
                 '<span class="op-act-lab">Using Squad Store'
                 '<span class="op-act-coord"> · Listing file</span></span></div>')
    session = dict(_SEEDED_SESSION, log=old_trace)
    ctx = browser.new_context()
    ctx.add_init_script(_seed_once_script(session))
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-log .op-act-lab")
        assert pg.locator("#op-log .op-act-lab").evaluate(
            "el => el.firstChild.nodeValue") == "Using host-app"
        assert pg.locator("#op-log .op-act-coord").inner_text() == \
            "· Listing file"
        assert pg.locator("#op-log .op-connector-ico").get_attribute("title") == \
            "Using host-app"
        assert errors == []
    finally:
        ctx.close()


def test_gpt_picker_offers_supported_reasoning_ladders(page, harness):
    """Every GPT option must render exactly the effort its runtime accepts.

    This drives the actual model selector and its change listener rather than
    inspecting the JavaScript table, so it catches a broken picker even if the
    mapping gets moved or refactored.
    """
    page.goto(harness.base + "/operator", wait_until="domcontentloaded")
    page.wait_for_function(
        "document.querySelector('#op-action-caret option[value=gpt]')",
        polling=100)
    page.evaluate("""() => {
        const driver = document.getElementById('op-action-caret');
        driver.value = 'gpt';
        driver.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_function(
        "document.querySelector('#op-model option[value=\\\"gpt-6-luna\\\"]')",
        polling=100)
    observed = page.evaluate("""() => {
        const model = document.getElementById('op-model');
        const effort = document.getElementById('op-effort');
        const out = {};
        for (const name of ['gpt-6-astra', 'gpt-6.1-sol', 'gpt-5.6-terra',
                            'gpt-6-luna']) {
          model.value = name;
          model.dispatchEvent(new Event('change'));
          out[name] = Array.from(effort.options, option => option.value);
        }
        return out;
    }""")
    delegated = ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert observed == {
        "gpt-6-astra": delegated,
        "gpt-6.1-sol": delegated,
        "gpt-5.6-terra": delegated,
        "gpt-6-luna": ["low", "medium", "high", "xhigh", "max"],
    }
    assert page.locator('#op-model option[value="gpt-5.5"]').count() == 0


def test_stale_default_model_response_cannot_overwrite_new_driver(page, harness):
    """A slow boot-time Claude roster must not win after GPT was selected."""
    harness.mode = "real"
    page.goto(harness.base + "/operator", wait_until="domcontentloaded")
    page.wait_for_function("typeof window._opLoadModels === 'function'", polling=100)
    page.evaluate("""async () => {
      const nativeFetch = window.fetch.bind(window);
      window.fetch = (...args) => {
        const url = String(args[0] || '');
        const response = nativeFetch(...args);
        if (!url.includes('/operator/models?driver=claude-a')) return response;
        return response.then(value => new Promise(resolve =>
          setTimeout(() => resolve(value), 500)));
      };
      await Promise.all([
        window._opLoadModels('claude-a'),
        window._opLoadModels('gpt'),
      ]);
    }""")
    assert page.locator('#op-model option[value="gpt-6-luna"]').count() == 1
    assert page.locator('#op-model option[value="claude-sonnet-5-5"]').count() == 0
    assert page._errors == [], f"JS errors: {page._errors}"


def test_placeholder_frames_not_treated_as_signal(page, harness):
    """Backend in the 2026-07-10 failure state: /frame serves HTTP 200
    PLACEHOLDER frames while /status reports error. Placeholders must not
    count as signal: the cockpit settles into the lost-signal overlay and holds it —
    no Connecting↔Reconnecting word flap, no stale/lost class strobing."""
    harness.mode = "real"
    page.clock.install()
    page.goto(harness.base + "/operator", wait_until="domcontentloaded")
    # give it two status polls (1.5s cadence) to reach the lost state
    page.wait_for_function(
        "document.getElementById('op').classList.contains('op-signal-lost')"
        " || document.getElementById('op').classList.contains('op-signal-stale')",
        timeout=8000, polling=100)
    page.clock.run_for(1500)            # let any flap start flapping
    samples = _sample_signal_state(page)  # 3s steady window
    words = [s["txt"] for s in samples]
    classes = [(s["stale"], s["lost"]) for s in samples]
    assert _transitions(words) <= 1, f"status word flaps: {words}"
    assert _transitions(classes) <= 1, f"signal classes strobe: {classes}"
    # placeholders never became "signal": with no frame yet, the overlay says Unable to connect
    last = samples[-1]
    assert last["lost"] and not last["stale"], f"expected lost overlay: {last}"
    assert page.eval_on_selector("#op-overlay-text",
                                 "el => el.textContent") == "Unable to connect"
    assert page.eval_on_selector("#op-view",
                                 "el => el.style.visibility") == "hidden"
    assert page._errors == [], f"JS errors: {page._errors}"


def test_stale_freeze_and_recovery(page, harness):
    """Live feed → signal drop → the stage FREEZES the last real frame
    (op-signal-stale; no full-screen overlay; feed stays visible) with a
    stable 'Reconnecting' card — then recovers to Ready when frames return."""
    harness.mode = "live"
    page.clock.install()
    page.goto(harness.base + "/operator", wait_until="domcontentloaded")
    page.wait_for_function(
        "document.getElementById('op').dataset.state === 'live'",
        timeout=8000, polling=100)
    page.clock.run_for(500)
    op_classes = page.eval_on_selector("#op", "el => el.className")
    assert "op-signal" not in op_classes, f"live but signal class set: {op_classes}"

    harness.mode = "dead"
    # Poll on a wall-clock interval: the 10fps blob feed plus session sync can
    # starve Playwright's default requestAnimationFrame polling in headless
    # Chromium even though the persistent class transition already happened.
    page.wait_for_function(
        "document.getElementById('op').classList.contains('op-signal-stale')",
        timeout=8000, polling=100)
    samples = _sample_signal_state(page, samples=14)  # ~2s steady window
    words = [s["txt"] for s in samples]
    assert _transitions(words) <= 1, f"status word flaps in stale mode: {words}"
    last = samples[-1]
    assert last["stale"] and not last["lost"], \
        f"expected frozen-frame mode, not overlay: {last}"
    assert words[-1] == "Reconnecting", f"card should read Reconnecting: {words}"
    assert page.locator(".op-overlay").is_hidden(), "reconnecting overlay covers the retained frame"
    # the last frame stays on stage — visible, not blanked
    assert page.eval_on_selector("#op-view",
                                 "el => el.style.visibility") != "hidden"

    harness.mode = "live"
    page.wait_for_function(
        "!document.getElementById('op').classList.contains('op-signal-stale')"
        " && !document.getElementById('op').classList.contains('op-signal-lost')",
        timeout=8000, polling=100)
    page.wait_for_function(
        "document.getElementById('op-action-txt').textContent.trim() === 'Ready'",
        timeout=8000, polling=100)
    assert page._errors == [], f"JS errors across drop/recover: {page._errors}"


def test_auto_mode_card_never_says_ready_over_a_dead_feed(browser, harness):
    """AUTO mode used to rewrite every idle card label to 'Ready', so a dead
    feed showed 'Ready' beside the still-spinning connection mark (the owner
    2026-10-06). The card follows the feed in both modes."""
    harness.mode = "live"
    ctx = browser.new_context()
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        "JSON.stringify({log: '', mode: 'auto'}))")
    pg = ctx.new_page()
    pg.clock.install()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op').dataset.state === 'live'"
            " && document.getElementById('op-action-txt').textContent.trim() === 'Ready'",
            timeout=8000, polling=100)
        harness.mode = "dead"
        pg.wait_for_function(
            "document.getElementById('op').classList.contains('op-signal-stale')",
            timeout=8000, polling=100)
        words = [s["txt"] for s in _sample_signal_state(pg, samples=10)]
        assert "Ready" not in words, f"card claims Ready over a dead feed: {words}"
        assert words[-1] == "Reconnecting", words
        harness.mode = "live"
        pg.wait_for_function(
            "document.getElementById('op-action-txt').textContent.trim() === 'Ready'",
            timeout=8000, polling=100)
        assert errors == [], errors
    finally:
        ctx.close()


def test_first_frame_waits_for_the_stage_size_answer(browser, harness):
    """The first picture used to be fetched before the cockpit's own size
    report landed, so every open showed the previous viewer's aspect for a
    second and then snapped (the owner 2026-10-06). The server captures a fresh
    frame at the new size before it answers, so the first fetch waits."""
    harness.mode = "live"
    harness.steer_delay = 0.8
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op').dataset.state === 'live'",
            timeout=8000, polling=100)
        kinds = [k for k, _ in harness.events]
        assert "stage_size" in kinds, kinds
        assert kinds.index("stage_size") < kinds.index("frame"), kinds
    finally:
        ctx.close()


def test_a_not_ready_answer_does_not_release_the_first_frame(browser, harness):
    """On a cold attach the size report beats the attach and is refused. That
    refusal released the hold, so the stale page-load size flashed before the
    retry landed (2026-10-06). Only a real answer releases it."""
    harness.mode = "live"
    harness.steer_not_ready = 2
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op').dataset.state === 'live'",
            timeout=8000, polling=100)
        kinds = [k for k, _ in harness.events]
        assert "stage_size" in kinds, kinds
        assert kinds.index("stage_size") < kinds.index("frame"), kinds
    finally:
        ctx.close()


def test_a_slow_stage_size_answer_cannot_hold_the_first_frame_for_long(browser, harness):
    harness.mode = "live"
    harness.steer_delay = 6.0
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    pg = ctx.new_page()
    try:
        import time as _t
        started = _t.monotonic()
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op').dataset.state === 'live'",
            timeout=8000, polling=100)
        first_frame = next(t for k, t in harness.events if k == "frame")
        assert first_frame - started < 3.5
    finally:
        ctx.close()


def test_leaving_the_homepage_reports_the_cockpit_stage(browser, harness):
    """AUTO mode's homepage owns the whole canvas; the cockpit that replaces it
    when a task starts has a rail and URL bar. That switch changes the stage
    without a window resize, so it reports the new size itself."""
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        "JSON.stringify({log: '', mode: 'auto'}))")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op').dataset.state === 'live'",
            timeout=8000, polling=100)
        pg.wait_for_timeout(1200)
        home = _stage_size(pg)
        pg.evaluate("document.getElementById('op-lp').hidden = true")
        pg.wait_for_function(
            f"(() => {{ const r = document.getElementById('op-stage')"
            f".getBoundingClientRect(); return Math.round(r.width) + 'x' + "
            f"Math.round(r.height) !== '{home}'; }})()", timeout=4000, polling=100)
        cockpit = _stage_size(pg)
        pg.wait_for_timeout(1500)
        assert harness.beacons and harness.beacons[-1] == cockpit, (harness.beacons, home, cockpit)
    finally:
        ctx.close()


def test_fresh_device_adopts_server_session(browser, harness):
    """The cross-device proof: a session written server-side (as if by another
    device) must appear in a completely fresh browser context — empty
    localStorage, first visit."""
    import json as _json
    import urllib.request
    marker = "cross-device-marker-7741"
    payload = _json.dumps({"data": {
        "log": f'<div class="op-msg user"><div class="bubble">{marker}</div></div>',
        "mode": "man", "bot": "", "model": "", "effort": ""}}).encode()
    req = urllib.request.Request(harness.base + "/operator/session",
                                 data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    assert _json.loads(urllib.request.urlopen(req).read())["ok"] is True

    harness.mode = "real"
    ctx = browser.new_context()          # fresh device: no localStorage at all
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            f"document.getElementById('op-log').textContent.includes({marker!r})",
            timeout=6000, polling=100)
        assert errors == [], f"JS errors adopting server session: {errors}"
    finally:
        ctx.close()


def test_open_device_adopts_a_remote_thread_update_without_reload(browser, harness):
    """A device already looking at a thread must receive another device's
    committed update; cross-device resume cannot depend on a hard refresh."""
    import json as _json
    import urllib.request

    first = _json.dumps({"conversation_id": "legacy", "data": {
        "log": '<div class="op-msg user"><div class="bubble">first device</div></div>',
        "mode": "man", "bot": "", "model": "", "effort": ""}}).encode()
    req = urllib.request.Request(harness.base + "/operator/session", data=first,
                                 method="POST", headers={"Content-Type": "application/json"})
    assert _json.loads(urllib.request.urlopen(req).read())["ok"] is True

    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op-log').textContent.includes('first device')",
            timeout=6000, polling=100)
        current = _json.loads(urllib.request.urlopen(
            harness.base + "/operator/session?conversation_id=legacy").read())
        second = _json.dumps({"conversation_id": "legacy",
                              "expected_rev": current["conversation_rev"],
                              "data": {
                                  "log": '<div class="op-msg user"><div class="bubble">continued elsewhere</div></div>',
                                  "mode": "man", "bot": "", "model": "", "effort": ""}}).encode()
        req = urllib.request.Request(harness.base + "/operator/session", data=second,
                                     method="POST", headers={"Content-Type": "application/json"})
        assert _json.loads(urllib.request.urlopen(req).read())["ok"] is True
        pg.wait_for_function(
            "document.getElementById('op-log').textContent.includes('continued elsewhere')",
            timeout=6000, polling=100)
        assert "continued elsewhere" in pg.locator("#op-log").inner_text(), {
            "log": pg.locator("#op-log").inner_text(),
            "cache": pg.evaluate("localStorage.getItem('operator-session-v2')"),
            "errors": errors,
        }
        assert errors == [], f"JS errors during remote adoption: {errors}"
    finally:
        ctx.close()


def test_second_device_observes_until_it_takes_over(browser, harness):
    """The same thread may be watched anywhere, but only one device edits it."""
    import json as _json
    import urllib.request

    payload = _json.dumps({"conversation_id": "legacy", "data": {
        "log": '<div class="op-msg user"><div class="bubble">shared thread</div></div>',
        "mode": "auto", "bot": "", "model": "", "effort": ""}}).encode()
    req = urllib.request.Request(harness.base + "/operator/session", data=payload,
                                 method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).read()

    first = browser.new_context(viewport={"width": 1280, "height": 800})
    second = browser.new_context(viewport={"width": 390, "height": 844})
    a, b = first.new_page(), second.new_page()
    errors = []
    a.on("pageerror", lambda exc: errors.append("a: " + str(exc)))
    b.on("pageerror", lambda exc: errors.append("b: " + str(exc)))
    try:
        a.goto(harness.base + "/operator", wait_until="domcontentloaded")
        a.wait_for_function("document.getElementById('op').dataset.threadControl === 'controller'",
                            timeout=7000, polling=100)
        b.goto(harness.base + "/operator", wait_until="domcontentloaded")
        b.wait_for_function("document.getElementById('op').dataset.threadControl === 'observer'",
                            timeout=7000, polling=100)
        b.wait_for_function("!document.getElementById('op').classList.contains('op-booting')",
                            timeout=7000, polling=100)
        banner = b.locator("#op-thread-observer")
        assert banner.is_visible(), banner.evaluate(
            "el => { const out=[]; for(let n=el;n;n=n.parentElement){const s=getComputedStyle(n);"
            "out.push({id:n.id, cls:n.className, hidden:n.hidden, display:s.display,"
            "visibility:s.visibility, rect:n.getBoundingClientRect().toJSON()});} return out; }") + errors
        assert b.locator("#op-input").is_disabled()
        # Chats are a launchpad action, not one more occupied slot in the
        # narrow browser brow. The trigger still exists for this device once
        # the user returns home.
        assert b.locator("#op-chats-open").count() == 0
        assert b.locator("#op-lp-chats").count() == 1

        # Both "devices" are tabs in one headless test browser. A real phone is
        # foregrounded when its user taps; mirror that first, otherwise Chromium
        # may defer the observer tab's fetch for several seconds. Dispatch avoids
        # Playwright's separate rAF-based physical-click stability wait.
        b.bring_to_front()
        b.locator("#op-thread-takeover").dispatch_event("click")
        b.wait_for_function("document.getElementById('op').dataset.threadControl === 'controller'",
                            timeout=5000, polling=100)
        assert not b.locator("#op-input").is_disabled()
        a.evaluate("window._opThreadHeartbeat(false)")
        a.wait_for_function("document.getElementById('op').dataset.threadControl === 'observer'",
                            timeout=7000, polling=100)
        assert a.locator("#op-input").is_disabled()
    finally:
        first.close()
        second.close()


def test_ipados_desktop_user_agent_is_labeled_ipad_and_takeover_is_prominent(
        browser, harness):
    """Modern iPad Safari identifies as Macintosh; touch capability unmasks it."""
    ipad = browser.new_context(
        viewport={"width": 1024, "height": 768},
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) "
                    "AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15"))
    ipad.add_init_script(
        "Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5})")
    other = browser.new_context(viewport={"width": 1280, "height": 800})
    a, b = ipad.new_page(), other.new_page()
    try:
        a.goto(harness.base + "/operator", wait_until="domcontentloaded")
        a.wait_for_function(
            "document.getElementById('op').dataset.threadControl === 'controller'",
            timeout=7000, polling=100)
        b.goto(harness.base + "/operator", wait_until="domcontentloaded")
        b.wait_for_function(
            "document.getElementById('op').dataset.threadControl === 'observer'",
            timeout=7000, polling=100)

        assert b.locator("#op-thread-observer-text").inner_text() == "iPad has control"
        style = b.locator("#op-thread-takeover").evaluate("""el => {
          const s = getComputedStyle(el), r = el.getBoundingClientRect();
          return {background:s.backgroundColor, height:r.height,
            weight:s.fontWeight, opacity:s.opacity};
        }""")
        assert style["background"] not in ("transparent", "rgba(0, 0, 0, 0)")
        assert style["height"] >= 28
        assert int(style["weight"]) >= 700
        assert float(style["opacity"]) == 1
    finally:
        ipad.close()
        other.close()


def test_mobile_handoff_take_control_opens_remote_keyboard(browser, harness):
    """The Take control tap is the iOS user gesture; spend it on the keyboard."""
    harness.mode = "live"
    harness.agent_mode = "running"
    import time as _time
    harness.agent_handoff = {"reason": "sign in, then continue", "ts": _time.time()}
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        # A fresh live cockpit deliberately starts in MAN. Put the synthetic
        # running turn into AUTO, matching the real state in which an agent can
        # emit a handoff request and the polling loop can surface its card.
        pg.locator("#op-mode .op-mode-btn[data-mode='auto']").dispatch_event("click")
        card = pg.locator(".op-handoff")
        card.wait_for(state="visible", timeout=7000)
        # Inspect synchronously inside the click handler. Headless Chromium may
        # blur a transparent textarea after the synthetic gesture returns, but
        # iOS decides whether to raise its keyboard during that gesture itself.
        takeover = card.locator(".op-takeover-btn").evaluate("""button => {
          button.click();
          return {
            active: document.activeElement && document.activeElement.id,
            keyboardOpen: document.getElementById('op').classList.contains(
              'op-keyboard-open')
          };
        }""")
        assert takeover == {"active": "op-key-capture", "keyboardOpen": True}
    finally:
        ctx.close()
        # Let already-issued presence POSTs finish, then remove this synthetic
        # two-device lease. Otherwise a late request from a closing context can
        # reclaim `legacy` after the autouse fixture cleared it for the next
        # test, making an unrelated control disabled for the lease duration.
        import time as _time
        _time.sleep(0.15)
        import operator_session as _osess
        with _osess._PRESENCE_LOCK:
            _osess._PRESENCE.clear()


def test_mode_toggle_pushes_session_to_server(browser, harness):
    """The push path: flipping MAN→AUTO saves the session, which must reach
    the server (debounced POST) — no agent dispatch involved."""
    import json as _json
    import urllib.request
    before = _json.loads(urllib.request.urlopen(
        harness.base + "/operator/session").read())["rev"]
    harness.mode = "real"
    ctx = browser.new_context()
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.bring_to_front()
        pg.wait_for_function("typeof window._opThreadHeartbeat === 'function'",
                             polling=100)
        pg.evaluate("window._opThreadHeartbeat(true)")
        pg.wait_for_function(
            "document.getElementById('op').dataset.threadControl === 'controller'",
            timeout=5000, polling=100)
        pg.locator("#op-mode .op-mode-btn[data-mode='auto']").dispatch_event("click")
        pg.wait_for_timeout(1800)        # debounce (600ms) + round-trip slack
        after = _json.loads(urllib.request.urlopen(
            harness.base + "/operator/session").read())
        assert after["rev"] > before, "mode toggle must push a new session rev"
        assert after["data"]["mode"] == "auto"
    finally:
        ctx.close()


@pytest.mark.parametrize("favicon_loads", [True, False], ids=["loaded", "fallback"])
def test_connector_action_uses_a_provider_favicon(page, harness, favicon_loads):
    """A raw connector call should read as its provider, not an MCP method."""
    harness.agent_mode = "running"
    harness.agent_messages = [{
        "ts": 2_000_000_000,
        "role": "action",
        "text": "Using Booking.com",
        "detail": "Searching accommodations",
    }]
    ctx = page.context
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    favicon_url = "https://www.google.com/s2/favicons?domain=booking.com&sz=32"
    if favicon_loads:
        page.route(favicon_url, lambda route: route.fulfill(
            content_type="image/svg+xml",
            body='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                 '<circle cx="8" cy="8" r="7"/></svg>'))
    else:
        page.route(favicon_url, lambda route: route.abort())
    try:
        page.goto(harness.base + "/operator", wait_until="domcontentloaded")
        # The harness keeps an off-screen trace clone for its responsive shell;
        # assert against the newest real row without turning that implementation
        # detail into a visibility requirement.
        row = page.locator(".op-connector-step").last
        row.wait_for(state="attached", timeout=8000)
        # Responsive layout may place the detail on its own visual line; the
        # semantic label is unchanged, so compare normalized rendered text.
        favicon = row.locator(".op-connector-favicon")
        if favicon_loads:
            favicon.wait_for(state="attached", timeout=8000)
            page.wait_for_function(
                "img => img.complete && img.naturalWidth > 0", arg=favicon.element_handle())
            assert favicon.count() == 1
            assert favicon.get_attribute("src") == favicon_url
            assert row.locator(".op-connector-fallback").count() == 0
            expected = "Using Booking.com · Searching accommodations"
        else:
            fallback = row.locator(".op-connector-fallback")
            fallback.wait_for(state="attached", timeout=8000)
            assert fallback.inner_text() == "B"
            assert favicon.count() == 0
            expected = "B Using Booking.com · Searching accommodations"
        assert " ".join(row.inner_text().split()) == expected
        assert "booking_com.accommodations_search_v2" not in row.inner_text()
        assert page._errors == [], f"JS errors during connector render: {page._errors}"
    finally:
        harness.agent_mode = None
        harness.agent_messages = []


def test_live_status_subline_does_not_flicker_against_job_checkpoint(browser, harness):
    import operator_workspace as ws
    run = ws.start_run('legacy', 'Verify a product')
    ws.update_job(run['run_id'], run['credential'], {
        'checkpoints': [{'step': 'Verify Canadian OTC fluoride strength', 'status': 'inProgress'}]})
    ws.request_approval(run['run_id'], run['credential'], {
        'kind': 'purchase', 'destination': 'example.com', 'description': 'One item'})
    ctx = browser.new_context(viewport={'width': 1000, 'height': 800})
    page = ctx.new_page(); harness.mode = 'live'
    page.route('**/operator/workspace?*', lambda route: route.fulfill(json=dict(ok=True, **ws.snapshot('legacy'))))
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_selector('.op-wb-approval')
        samples = page.evaluate("""() => {
          const sub = document.getElementById('op-action-sub');
          sub.textContent = 'Reading';
          const samples = [];
          for (let i = 0; i < 12; i++) {
            window.OperatorWorkbench.update({alive:true,run_id:'test'}, 'legacy');
            samples.push(sub.textContent);
          }
          return samples;
        }""")
        assert samples == ['Reading'] * 12
        assert page.locator('#op-action-sub').evaluate('el => getComputedStyle(el).animationName') == 'none'
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()


@pytest.mark.parametrize('kind', ['wrapped', 'wide', 'result'])
def test_code_block_wheel_keeps_native_transcript_scroll(browser, harness, kind):
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 1000, 'height': 800})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page()
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        # Discovery and font layout can still move the transcript after DOM load.
        page.wait_for_function("typeof window._opRunSavedTask === 'function'")
        page.evaluate('document.fonts.ready')
        page.wait_for_function("document.getElementById('op-lp-x')._wired === true")
        page.locator('#op-lp-x').dispatch_event('click')
        page.evaluate('''kind => {
          const log = document.getElementById('op-log');
          // Touching the log cancels the boot-time pin to the bottom, which
          // could otherwise land after the block is centred and scroll it away.
          log.dispatchEvent(new PointerEvent('pointerdown'));
          log.replaceChildren();
          const spacer = () => {
            const el = document.createElement('div');
            el.style.cssText = 'flex:0 0 700px';
            return el;
          };
          const host = document.createElement('div');
          host.className = kind === 'result' ? 'op-workbench op-wb-rich' : 'op-msg bot';
          const bubble = document.createElement('div');
          if (kind !== 'result') bubble.className = 'bubble';
          const pre = document.createElement('pre');
          if (kind === 'wide') pre.className = 'op-pre-table';
          const code = document.createElement('code');
          code.textContent = kind === 'wide' ? 'Column '.repeat(90)
            : Array.from({length: 8}, (_, i) => 'Line ' + i).join('\\n');
          pre.append(code); bubble.append(pre); host.append(bubble);
          log.append(spacer(), host, spacer());
          window.__codeWheel = null;
          pre.addEventListener('wheel', e => {
            window.__codeWheel = {prevented:e.defaultPrevented, delta:e.deltaY};
          });
        }''', kind)
        # Boot can reopen the splash after the close click, hiding the chat
        # column until the log's new children close it again; centring before
        # then does nothing and leaves the block below the viewport. The close
        # also starts ~0.25 s height and margin transitions, and a block
        # measured mid-transition drifted 3-6 px, enough to put the pointer on
        # the spacer under the one-line [wide] block (IN-0034). So centre in a
        # settled layout and measure once the block holds still.
        page.wait_for_function("document.getElementById('op-lp').hidden"
                               " && document.getElementById('op-log').clientHeight > 0")
        page.wait_for_function("document.getAnimations().every(a => a.playState !== 'running'"
                               " || (a.effect && a.effect.getTiming().iterations === Infinity))")
        page.evaluate("document.querySelector('#op-log pre').scrollIntoView({block: 'center'})")
        page.wait_for_function('''() => new Promise(done => {
          const block = document.querySelector('#op-log pre');
          const y = block.getBoundingClientRect().y;
          requestAnimationFrame(() => requestAnimationFrame(() =>
            done(block.getBoundingClientRect().y === y)));
        })''')
        pre = page.locator('#op-log pre')
        rect = pre.bounding_box()
        assert rect and rect['width'] > 0
        before = page.locator('#op-log').evaluate('e => e.scrollTop')
        page.mouse.move(rect['x'] + rect['width'] / 2, rect['y'] + min(rect['height'] / 2, 20))
        page.mouse.wheel(0, 200)
        # The passive wheel scroll can commit on the compositor before the page
        # runs the listener, so the scroll alone does not prove the event
        # reached the block. Wait for the listener, then for the scroll.
        page.wait_for_function('window.__codeWheel !== null', timeout=3000)
        assert page.evaluate('window.__codeWheel.prevented') is False
        page.wait_for_function('''before => document.getElementById('op-log').scrollTop > before + 100''', arg=before, timeout=3000)
        if kind == 'wide':
            assert pre.evaluate('e => e.scrollWidth > e.clientWidth')
            pre.scroll_into_view_if_needed()
            rect = pre.bounding_box()
            page.mouse.move(rect['x'] + rect['width'] / 2, rect['y'] + min(rect['height'] / 2, 20))
            left = pre.evaluate('e => e.scrollLeft')
            page.mouse.wheel(180, 0)
            page.wait_for_function('''left => document.querySelector('#op-log pre').scrollLeft > left''', arg=left, timeout=3000)
    finally:
        ctx.close()


def test_code_block_touch_pan_scrolls_transcript(browser, harness):
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 1000, 'height': 800}, has_touch=True)
    ctx.add_init_script("localStorage.setItem('operator-session-v2', JSON.stringify({log:'',mode:'auto'}))")
    page = ctx.new_page()
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.evaluate("document.getElementById('op-lp').style.display = 'none'")
        page.evaluate('''() => {
          const log = document.getElementById('op-log');
          log.innerHTML = '<div style="flex:0 0 700px"></div>'
            + '<div class="op-msg bot"><div class="bubble"><pre><code>'
            + 'line 1\\nline 2\\nline 3\\nline 4\\nline 5\\nline 6'
            + '</code></pre></div></div><div style="flex:0 0 700px"></div>';
        }''')
        page.evaluate("document.getElementById('op-log').scrollTop = 650")
        pre = page.locator('#op-log pre')
        rect = pre.bounding_box()
        assert rect['y'] < 800, rect
        before = page.locator('#op-log').evaluate('e => e.scrollTop')
        x = rect['x'] + rect['width'] / 2
        y = rect['y'] + rect['height'] / 2
        session = ctx.new_cdp_session(page)
        session.send('Input.dispatchTouchEvent', {'type': 'touchStart',
            'touchPoints': [{'x': x, 'y': y}]})
        for step in range(1, 6):
            session.send('Input.dispatchTouchEvent', {'type': 'touchMove',
                'touchPoints': [{'x': x, 'y': y - step * 16}]})
        session.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        page.wait_for_timeout(250)
        geometry = page.evaluate('''([x,y]) => {
          const log = document.getElementById('op-log');
          const pre = log.querySelector('pre');
          return {after:log.scrollTop, client:log.clientHeight, scroll:log.scrollHeight,
            touchAction:getComputedStyle(pre).touchAction,
            target:document.elementFromPoint(x,y)?.tagName};
        }''', [x, y])
        assert geometry['after'] > before + 20, (before, rect, geometry)
    finally:
        ctx.close()


def test_manual_mode_waits_for_server_takeover_boundary(browser, harness):
    """MAN during a live turn stays pending until the server has stopped the
    run at a tool boundary; it must not merely repaint AUTO as MAN."""
    state = {"value": "running", "takeovers": 0}
    ctx = browser.new_context()
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()

    def agent_routes(route):
        req = route.request
        if req.url.endswith("/operator/agent/takeover"):
            state["takeovers"] += 1
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "pending": True,
                                           "timeout_s": 12}))
            return
        if "/operator/agent?" in req.url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({
                              "bot": "gpt", "task": "book it",
                              "state": state["value"], "messages": [],
                              "final": "", "alive": state["value"] == "running",
                              "stalled": False, "stalled_for": 0,
                              "handoff": None, "surface": "browser",
                              "steer_pending": 0, "tool_active": True}))
            return
        route.continue_()

    # Playwright's trailing `*` does not cross the slash in `/agent/takeover`.
    # Register the mutating seam explicitly and keep the query route separate.
    pg.route("**/operator/agent/takeover", agent_routes)
    pg.route("**/operator/agent?**", agent_routes)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op-send').classList.contains('stopping')",
            timeout=8000, polling=100)
        pg.locator('.op-mode-btn[data-mode="man"]').dispatch_event("click")
        pg.wait_for_function("document.getElementById('op-mode').dataset.pending === 'man'",
                             timeout=3000, polling=50)
        assert pg.locator("#op").get_attribute("data-mode") == "auto"
        assert state["takeovers"] == 1
        pg.wait_for_function(
            "parseFloat(getComputedStyle(document.getElementById('op-mode'), '::after').opacity) > .9",
            timeout=1000, polling=25)
        pending_visual = pg.locator("#op-mode").evaluate("""el => {
          const label = el.querySelector('[data-mode="man"]');
          const queue = getComputedStyle(el, '::after');
          return {labelAnimation: getComputedStyle(label).animationName,
            queueOpacity: queue.opacity, queueAnimation: queue.animationName};
        }""")
        assert pending_visual["labelAnimation"] == "none", pending_visual
        assert float(pending_visual["queueOpacity"]) > 0.9, pending_visual
        assert pending_visual["queueAnimation"] == "op-mode-takeover-sweep", pending_visual

        state["value"] = "interrupted"
        pg.wait_for_function("document.getElementById('op').dataset.mode === 'man'",
                             timeout=5000, polling=100)
        assert pg.locator("#op-mode").get_attribute("data-pending") is None
    finally:
        ctx.close()


def test_slash_picker_attaches_a_removable_connector_without_pasting_or_sending(browser, harness):
    ctx = browser.new_context(viewport={"width": 1200, "height": 800})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps(_SEEDED_SESSION)) + ");")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        _token_host_styles(pg)
        pg.wait_for_selector("#op-input", state="visible", timeout=8000)
        field = pg.locator("#op-input")
        field.fill("Check /wiki")
        pg.wait_for_selector(".op-resource-row", state="visible", timeout=5000)
        assert pg.locator(".op-resource-row").count() == 1
        assert pg.locator(".op-resource-row .op-resource-name").text_content() == "Wikipedia"
        output = os.environ.get('CC_UI_QA_OUTPUT')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            pg.locator('#op-resource-menu').screenshot(path=str(Path(output) / 'connector-menu.png'))
        pg.press("#op-input", "Enter")
        assert field.input_value() == "Check "
        assert pg.locator('#op-inputbox-resources .op-resource-chip').count() == 1
        assert 'Wikipedia' in pg.locator('#op-inputbox-resources').inner_text()
        assert harness.dispatch_posts == []
        if output:
            pg.locator('.op-inputbox').screenshot(path=str(Path(output) / 'connector-chip.png'))
        field.fill('Check /wiki')
        pg.wait_for_selector('.op-resource-row', state='visible')
        pg.press('#op-input', 'Enter')
        assert pg.locator('#op-inputbox-resources .op-resource-chip').count() == 1
        pg.get_by_role('button', name='Remove Wikipedia', exact=True).first.click()
        assert pg.locator('#op-inputbox-resources .op-resource-chip').count() == 0
        field.fill("Check /")
        pg.wait_for_selector(".op-resource-row", state="visible")
        assert pg.locator(".op-resource-row").count() > 1
        pg.press("#op-input", "Escape")
        assert pg.locator("#op-resource-menu").is_hidden()
        assert field.input_value() == "Check /"
        assert errors == [], f"JS errors: {errors}"
    finally:
        ctx.close()


def test_slash_picker_keeps_mcp_attached_on_reload_and_sends_it_as_context(browser, harness):
    ctx = browser.new_context(viewport={"width": 1200, "height": 800})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps(_SEEDED_SESSION)) + ");")
    pg = ctx.new_page()
    pg.route("**/operator/resources?bot=gpt", lambda route: route.fulfill(
        json={"resources": [{"kind": "mcp", "name": "vecgrep",
                             "domain": "", "insert": "the vecgrep MCP"}]}))
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-action-caret option[value=gpt]", state="attached", timeout=8000)
        pg.select_option("#op-action-caret", "gpt")
        field = pg.locator("#op-input")
        field.fill("Search with @vec")
        pg.wait_for_selector(".op-resource-row", state="visible", timeout=5000)
        assert pg.locator(".op-resource-kind").text_content() == "MCP"
        pg.press("#op-input", "Enter")
        assert field.input_value() == "Search with "
        assert 'vecgrep' in pg.locator('#op-inputbox-resources').inner_text()
        assert harness.dispatch_posts == []
        pg.reload(wait_until='domcontentloaded')
        pg.wait_for_selector('#op-inputbox-resources .op-resource-chip', timeout=8000)
        field = pg.locator('#op-input')
        field.fill('Find the document')
        pg.press('#op-input', 'Enter')
        pg.wait_for_function('document.querySelectorAll("#op-log .op-msg.user").length > 0')
        for _ in range(40):
            if harness.dispatch_posts:
                break
            pg.wait_for_timeout(50)
        assert len(harness.dispatch_posts) == 1
        assert 'the vecgrep MCP' in harness.dispatch_posts[0]['task']
        assert 'Find the document' in harness.dispatch_posts[0]['task']
        assert pg.locator('#op-inputbox-resources .op-resource-chip').count() == 0
        assert errors == [], f"JS errors: {errors}"
    finally:
        ctx.close()


def test_touch_stage_requires_explicit_keyboard_control(browser, harness):
    """A browser tap must steer without summoning iOS's keyboard; typing is explicit."""
    ctx = browser.new_context(
        viewport={"width": 820, "height": 1180},
        has_touch=True,
        is_mobile=True,
    )
    pg = ctx.new_page()
    pg.set_default_timeout(8000)

    def record_steer(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "url": "https://example.com"}))

    pg.route("**/operator/steer", record_steer)
    harness.mode = "live"
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op-view').naturalWidth > 0",
            timeout=8000, polling=50)
        pg.locator("#op-lp").evaluate("el => { el.hidden = true; }")
        # The harness begins in the transitional idle state, whose connection
        # veil correctly sits above every stage control. This test owns the
        # live-browser interaction contract, so clear that unrelated veil.
        pg.locator("#op-overlay").evaluate("el => { el.style.display = 'none'; }")
        # Chromium can wait indefinitely for a compositor frame while
        # Playwright's touchscreen.tap drives a headless mobile context. Send
        # the same DOM touch sequence directly: this test owns the touch
        # handler/focus contract, not Chromium's input-device transport.
        # The stage ignores a tap while the frame pump has no decoded frame,
        # so the tap point and frame are read in the same page step, and the
        # tap repeats until the stage takes focus. A box measured beforehand
        # could meet a frame mid-swap and leave focus on <body> (IN-0059).
        pg.wait_for_function("""() => {
          const el = document.getElementById('op-stage');
          if (!document.getElementById('op-view').naturalWidth) return false;
          const r = el.getBoundingClientRect();
          const touch = new Touch({identifier:1, target:el,
            clientX:r.left + r.width / 2, clientY:r.top + r.height / 2});
          el.dispatchEvent(new TouchEvent('touchstart', {
            bubbles:true, cancelable:true, touches:[touch], targetTouches:[touch],
            changedTouches:[touch]}));
          el.dispatchEvent(new TouchEvent('touchend', {
            bubbles:true, cancelable:true, touches:[], targetTouches:[],
            changedTouches:[touch]}));
          return document.activeElement === el;
        }""", timeout=8000, polling=100)
        # A normal browser tap focuses the non-editable stage for hardware-key
        # handling, but must not focus the hidden textarea and make iOS raise
        # the software keyboard over the page the user just tapped.
        assert pg.evaluate("document.activeElement.id") == "op-stage"

        # Mobile typing remains available, deliberately, from the visible
        # keyboard control rather than as an accidental consequence of click.
        key_state = pg.locator("#op-keyboard").evaluate("""el => {
          const r = el.getBoundingClientRect(), s = getComputedStyle(el);
          return {display:s.display, width:r.width, height:r.height};
        }""")
        assert key_state["display"] != "none" and key_state["width"] >= 32, key_state
        # The stage frame is intentionally repainted very frequently, so a
        # physical Playwright click may never satisfy its stillness heuristic.
        # As above, exercise the DOM event contract directly.
        pg.locator("#op-keyboard").dispatch_event("click")
        pg.wait_for_function(
            "document.activeElement.id === 'op-key-capture'",
            timeout=8000, polling=50)
        # Keyboard mode deliberately compacts the rail before iOS has finished
        # animating its keyboard. Otherwise the fixed half-sheet gets lifted
        # above the keyboard and covers the entire remaining browser viewport.
        keyboard_layout = pg.evaluate("""() => {
          const op = document.getElementById('op');
          const rail = document.querySelector('.op-rail').getBoundingClientRect();
          const stage = document.getElementById('op-stage').getBoundingClientRect();
          return {open: op.classList.contains('op-keyboard-open'),
            railH: rail.height, stageH: stage.height};
        }""")
        assert keyboard_layout["open"], keyboard_layout
        assert keyboard_layout["railH"] <= 205, keyboard_layout
        assert keyboard_layout["stageH"] >= 900, keyboard_layout

        # Mobile Safari can deliver software-keyboard text as an input event
        # without a useful keydown. Exercise that path directly.
        with pg.expect_request(lambda r: (
                r.url.endswith("/operator/steer")
                and r.post_data_json.get("kind") == "type")) as sent:
            pg.locator("#op-key-capture").evaluate("""el => {
              el.value = 'hello';
              el.dispatchEvent(new InputEvent('input', {
                bubbles: true, inputType: 'insertText', data: 'hello'
              }));
            }""")
        assert sent.value.post_data_json["value"] == "hello"
    finally:
        harness.mode = "real"
        ctx.close()


def test_mobile_minimized_status_keeps_manual_note_close(browser, harness):
    """The manual note follows the compact status without a reserved void."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        # A previous harness page can legitimately leave the isolated shared
        # session in its collapsed launchpad state. This test sets the exact
        # minimized-status geometry below, so require the shell to exist rather
        # than accidentally coupling the assertion to suite execution order.
        pg.wait_for_selector("#op-lp", state="attached", timeout=8000)
        pg.evaluate("document.fonts.ready")
        # This pins final spacing, not the intermediate opening animation.
        pg.add_style_tag(content="*,*::before,*::after{transition:none!important;animation:none!important}")
        geometry = pg.evaluate("""() => {
          const op = document.getElementById('op');
          op.classList.remove('op-booting'); op.classList.add('op-ready');
          op.dataset.mode = 'man'; op.dataset.statusMin = '1';
          document.getElementById('op-lp').hidden = true;
          document.getElementById('op-man-note').hidden = false;
          void op.offsetHeight;
          const pill = document.querySelector('.op-action').getBoundingClientRect();
          const note = document.getElementById('op-man-note').getBoundingClientRect();
          return {gap: note.top - pill.bottom, pill, note};
        }""")
        assert 4 <= geometry["gap"] <= 14, geometry
    finally:
        ctx.close()


def test_desktop_stage_keeps_hardware_keyboard_input(browser, harness):
    """The iOS capture path must not replace ordinary stage focus on desktop."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()

    def record_steer(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "url": "https://example.com"}))

    pg.route("**/operator/steer", record_steer)
    harness.mode = "live"
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function(
            "document.getElementById('op-view').naturalWidth > 0",
            timeout=8000, polling=50)
        pg.locator("#op-lp").evaluate("el => { el.hidden = true; }")
        # The persisted shell can restore AUTO. Put the real mode control in
        # MAN before asserting the manual pointer feedback.
        pg.locator('.op-mode-btn[data-mode="man"]').dispatch_event("click")
        pg.wait_for_function(
            "document.getElementById('op').dataset.mode === 'man'",
            timeout=3000, polling=50)
        # Exercise the stage's desktop click handler without making this
        # keyboard-path test depend on headless compositor stability.
        pg.locator("#op-stage").evaluate("""el => {
          const r=el.getBoundingClientRect();
          el.dispatchEvent(new MouseEvent('click', {bubbles:true,
            clientX:r.left+100, clientY:r.top+100, detail:1}));
        }""")
        assert pg.evaluate("document.activeElement.id") == "op-stage"
        # A browser-stage click gets an immediate local pointer while the next
        # streamed frame catches up. Without this, the cursor looks stuck at
        # its old location even though the remote click did arrive.
        assert pg.locator("#op-steer-cursor").evaluate(
            "el => el.classList.contains('show') "
            "&& getComputedStyle(el).display !== 'none'")

        with pg.expect_request(lambda r: (
                r.url.endswith("/operator/steer")
                and r.post_data_json.get("kind") == "type")) as sent:
            pg.keyboard.type("x")
        assert sent.value.post_data_json["value"] == "x"
    finally:
        harness.mode = "real"
        ctx.close()


@pytest.mark.parametrize("touch", [False, True], ids=["stage", "touch-keyboard"])
def test_system_clipboard_pastes_once_into_focused_remote_stage(browser, harness, touch):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800},
                              permissions=["clipboard-read", "clipboard-write"],
                              has_touch=touch, is_mobile=touch)
    pg = ctx.new_page()
    sent = []

    def record_steer(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "url": "https://example.com"}))

    pg.route("**/operator/steer", record_steer)
    harness.mode = "live"
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.locator("#op-lp").evaluate("el => { el.hidden = true; }")
        pg.evaluate("navigator.clipboard.writeText('one\\ntwo \\u00e9')")
        pg.locator("#op-key-capture" if touch else "#op-stage").focus()
        with pg.expect_response(lambda r: r.url.endswith("/operator/steer")
                                and r.request.post_data_json.get("kind") == "paste"):
            # Clipboard access is local to the test browser, so use the host
            # modifier. The asserted remote action remains a plain paste.
            pg.keyboard.press("ControlOrMeta+v")
        assert [a for a in sent if a.get("kind") == "paste"] == [
            {"kind": "paste", "value": "one\ntwo \u00e9"}]
        assert not [a for a in sent if a.get("kind") == "type"]
    finally:
        ctx.close()


def test_https_lock_does_not_keep_an_unrelated_input_error(browser, harness):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()

    def steer(route):
        value = route.request.post_data_json.get("value")
        result = ({"ok": False, "error": "input failed"} if value == "x"
                  else {"ok": True, "url": "https://www.marriott.com/"})
        route.fulfill(content_type="application/json", body=json.dumps(result))

    pg.route("**/operator/steer", steer)
    harness.mode = "live"
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.locator("#op-lp").evaluate("el => { el.hidden = true; }")
        pg.locator("#op-stage").focus()
        with pg.expect_response(lambda r: r.url.endswith("/operator/steer")
                                and r.request.post_data_json.get("value") == "x"):
            pg.keyboard.type("x")
        pg.wait_for_timeout(100)
        assert not pg.locator("#op-dotstat").evaluate(
            "el => el.classList.contains('err')")

        pg.locator("#op-dotstat").evaluate("el => el.classList.add('err')")
        pg.keyboard.type("y")
        pg.wait_for_function("document.getElementById('op-url').value === "
                             "'https://www.marriott.com/'", timeout=3000)
        assert not pg.locator("#op-dotstat").evaluate(
            "el => el.classList.contains('err')")
    finally:
        ctx.close()


def test_restored_session_starts_at_chat_bottom(browser, harness):
    """Refreshing a long conversation opens on its newest message, after the
    boot/layout reflow has settled."""
    log = "".join(
        f'<div class="op-msg bot"><div class="bubble">message {i}<br>'
        + ("long restored line " * 12)
        + "</div></div>"
        for i in range(60)
    )
    session = dict(_SEEDED_SESSION, log=log)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps(session)) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-log .op-msg", state="attached", timeout=8000)
        pg.wait_for_timeout(700)
        distance = pg.locator("#op-log").evaluate(
            "el => el.scrollHeight - el.scrollTop - el.clientHeight")
        assert distance <= 2, f"restored chat opened {distance}px above bottom"
    finally:
        ctx.close()


@pytest.mark.parametrize('arrival', ['completed', 'running', 'late_final', 'already_saved'])
def test_observer_reconciles_final_without_replaying_or_taking_control(browser, harness, arrival):
    harness.mode = 'live'
    answer = '**Available flights**\n\n- Tuesday\n- Thursday'
    log_html = '<div class="op-msg user"><div class="bubble">Find flights</div></div>'
    log_html += '<div class="op-task"><div class="op-task-step">Reading flight results</div></div>'
    if arrival == 'already_saved':
        log_html += '<div class="op-msg bot"><div class="bubble"><p><strong>Available flights</strong></p><ul><li>Tuesday</li><li>Thursday</li></ul></div></div>'
    session = {'log': log_html, 'mode': 'auto', 'bot': '', 'model': '', 'effort': ''}
    snapshot = {'state': 'running' if arrival == 'running' else 'done',
                'final': '' if arrival in ('running', 'late_final') else answer,
                'messages': [], 'bot': 'gpt', 'alive': True,
                'started_ts': 1, 'ended_ts': 2, 'run_id': 'observer-fixture'}
    ctx = browser.new_context(viewport={'width': 390 if arrival == 'late_final' else 1440, 'height': 900})
    ctx.add_init_script(_seed_once_script(session))
    pg = ctx.new_page()
    errors, writes = [], []
    cleared = {'done': False}
    pg.on('pageerror', lambda error: errors.append(str(error)))
    pg.on('request', lambda req: writes.append(req.url) if req.method == 'POST' else None)
    # the clear button deletes the conversation and reloads; after that the
    # server holds a fresh empty one, so the session route must say so
    pg.route('**/operator/session', lambda route: route.fulfill(json={
        'ok': True, 'data': None if cleared['done'] else session, 'rev': 1,
        'conversation_rev': 1, 'conversation_id': 'fresh-1' if cleared['done'] else 'legacy'}))
    pg.route('**/operator/sessions/*?fresh=1', lambda route: (
        cleared.update(done=True), route.fulfill(json={'ok': True, 'active': 'fresh-1', 'rev': 2})))
    pg.route('**/operator/sessions/*/presence', lambda route: route.fulfill(json={
        'ok': True, 'can_control': False, 'controller_label': 'HOST-B'}))
    idle = {'state': 'idle', 'final': '', 'messages': [], 'bot': '', 'alive': False,
            'started_ts': 0, 'ended_ts': 0, 'run_id': ''}
    pg.route('**/operator/agent?*', lambda route: route.fulfill(
        json=idle if cleared['done'] else snapshot))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.wait_for_selector('#op[data-thread-control="observer"]')
        assert 'HOST-B has control' in pg.locator('.op-thread-observer').inner_text()
        if arrival == 'running':
            pg.wait_for_selector('#op[data-busy="1"]')
        if arrival in ('running', 'late_final'):
            pg.wait_for_timeout(1700)  # previously-handled terminal state, or running poll
            snapshot.update(state='done', final=answer)
        bubble = pg.locator('.op-msg.bot .bubble').filter(has_text='Available flights')
        bubble.wait_for(timeout=8000)
        pg.wait_for_timeout(1800)  # repeated done polls must remain idempotent
        assert bubble.count() == 1
        assert bubble.locator('li').count() == 2
        assert 'returned no summary' not in pg.locator('#op-log').inner_text()
        assert not any('/agent/stop' in url or '/dispatch' in url for url in writes)
        assert not errors, errors
        color = pg.locator('.op-thread-observer button').evaluate('el => getComputedStyle(el).backgroundColor')
        assert color == 'rgb(165, 173, 187)'
        if arrival == 'already_saved':
            # Exercise the clear handler with a delayed server reset. Never
            # send the fixture's reset to an actual runner.
            pg.route('**/operator/agent/reset', lambda route: route.fulfill(json={'ok': True}))
            pg.locator('#op-clear').evaluate('el => el.click()')
            pg.wait_for_function(_RELOADED, timeout=20000)     # the reload landed
            pg.wait_for_selector('#op-log', state='attached', timeout=8000)
            pg.wait_for_timeout(1500)
            assert cleared['done'], 'clear must delete the conversation on the server'
            assert pg.locator('.op-msg.bot .bubble').count() == 0
    finally:
        ctx.close()


def test_retained_history_fills_gaps_and_loads_older_without_duplicates(browser, harness):
    harness.mode = 'live'
    session = {'mode': 'auto', 'log': '<div class="op-msg user"><span class="bubble">Question 2</span></div><div class="op-task">Existing thinking trace</div>'}
    turns = [{'id': i, 'task': f'Question {i}', 'final': f'**Answer {i}**', 'state': 'done'} for i in (1, 2)]
    ctx = browser.new_context(viewport={'width': 390, 'height': 900})
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda error: errors.append(str(error)))
    pg.route('**/operator/session', lambda r: r.fulfill(json={'ok': True, 'data': session, 'rev': 1, 'conversation_rev': 1, 'conversation_id': 'legacy'}))
    def history(route):
        if 'before=' in route.request.url:
            data = {'turns': [{'id': 0, 'task': 'Question 0', 'final': 'Answer 0'}], 'oldest_id': 0, 'has_more': False}
        elif 'after=2' in route.request.url:
            data = {'turns': [], 'latest_id': 2, 'has_more': False}
        else:
            data = {'turns': turns, 'oldest_id': 1, 'latest_id': 2, 'has_more': True}
        route.fulfill(json={'ok': True, **data})
    pg.route('**/operator/history?*', history)
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.locator('.op-msg.bot').filter(has_text='Answer 2').wait_for()
        assert pg.locator('.op-msg.user').count() == 2
        assert 'Existing thinking trace' in pg.locator('#op-log').inner_text()
        pg.locator('.op-load-earlier').click()
        pg.locator('.op-msg.bot').filter(has_text='Answer 0').wait_for()
        assert pg.locator('.op-load-earlier').count() == 0
        # A stale controller cache arrives again; retained history repairs it.
        pg.evaluate('data => window._opApplySession(data, 2, true, "legacy", 2)', session)
        pg.wait_for_timeout(300)
        assert pg.locator('.op-msg.bot').count() == 3
        assert pg.locator('.op-msg.user').count() == 3
        assert pg.locator('.op-msg .bubble').all_text_contents() == [
            'Question 0', 'Answer 0', 'Question 1', 'Answer 1', 'Question 2', 'Answer 2']
        # Clear on another device must invalidate this viewer's cached pages.
        pg.unroute('**/operator/history?*')
        pg.route('**/operator/history?*', lambda r: r.fulfill(json={
            'ok': True, 'turns': [], 'cleared_through_id': 2, 'has_more': False}))
        fresh = {'mode': 'auto', 'log': '<div class="op-msg user"><span class="bubble">Fresh start</span></div>'}
        pg.evaluate('data => window._opApplySession(data, 3, true, "legacy", 3)', fresh)
        pg.wait_for_timeout(300)
        assert pg.locator('.op-msg .bubble').all_text_contents() == ['Fresh start']
        assert not errors, errors
    finally:
        ctx.close()


def test_deferred_viewport_beacon_retries_until_server_applies(browser, harness):
    """Accepted-but-deferred is pending, not a successfully applied viewport."""
    import time
    ctx = _restored_ctx(browser, viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    requests = []
    def steer(route):
        payload = route.request.post_data_json
        if payload.get("kind") == "stage_size":
            requests.append(payload)
            result = {"ok": True, "applied": len(requests) > 1}
        else:
            result = {"ok": True}
        route.fulfill(content_type="application/json", body=json.dumps(result))
    pg.route("**/operator/steer", steer)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        deadline = time.monotonic() + 5
        while len(requests) < 2 and time.monotonic() < deadline:
            pg.wait_for_timeout(50)
        assert len(requests) >= 2, "deferred beacon was never retried"
        # Bootstrap can settle the stage between sends. The retry must carry
        # its latest desired dimensions, not stale initial fullscreen bounds.
        expected = pg.locator("#op-stage").evaluate(
            "el => {const r=el.getBoundingClientRect();return Math.round(r.width)+'x'+Math.round(r.height)}")
        assert requests[-1]["value"] == expected
        assert requests[-1]["force"] is True
        settled = len(requests)
        pg.wait_for_timeout(1800)
        assert len(requests) == settled, "applied viewport should stop retrying"
    finally:
        ctx.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_open_browser_stays_connecting_until_feed_attachment(browser, harness, width):
    harness.mode = 'live'
    status = dict(_STATUS_DEAD, status='connecting', browser_up=None,
                  detail='retrying browser connection…')
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda error: errors.append(str(error)))
    calls = []
    def reply(route):
        calls.append(1)
        if len(calls) > 1:
            status['browser_up'] = True
        route.fulfill(json=status)
    pg.route('**/operator/status', reply)
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.wait_for_function("document.getElementById('op').dataset.connection === 'connecting' && document.getElementById('op').dataset.state === 'connecting'")
        pg.wait_for_timeout(700)
        assert status['browser_up'] is True
        assert status['has_frame'] is False
        assert pg.locator('#op-lp-mark').get_attribute('aria-label') == 'Connecting to browser…'
        status.update(_STATUS_LIVE)
        pg.wait_for_selector('#op[data-connection="live"]', state='attached')
        assert pg.locator('#op-lp-mark').get_attribute('aria-label') == 'Connected'
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize('frames', ['running', 'suspended'])
@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('slow_layout', [False, True])
def test_status_keeps_tokens_compact_full_action_and_one_chat_trace(browser, harness, width, frames, slow_layout):
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    session = {'log': '', 'mode': 'auto', 'bot': '', 'model': '', 'effort': ''}
    ctx.add_init_script(_seed_once_script(session))
    # Same display-sleep condition as the mobile composer test (INC-0023).
    if frames == 'suspended':
        ctx.add_init_script('window.requestAnimationFrame = () => 0;')
    pg = ctx.new_page()
    # This geometry case supplies its own status DOM. An idle agent response
    # can clear those rows while we wait for the card's real CSS transitions.
    pg.route('**/operator/agent?*', lambda route: route.fulfill(status=204))
    pg.route('**/operator/session', lambda route: route.fulfill(json={
        'ok': True, 'data': session, 'rev': 1, 'conversation_rev': 1, 'conversation_id': 'legacy'}))
    pg.route('**/operator/session?*', lambda route: route.fulfill(status=204))
    errors = []
    pg.on('pageerror', lambda error: errors.append(str(error)))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.wait_for_function('!!window.OperatorWorkbench')
        # The boot splash hides the rail until boot finishes; interval polling
        # keeps the wait working with frames suspended.
        pg.wait_for_function("!document.getElementById('op').classList.contains('op-booting')",
                             polling=50)
        if slow_layout:
            # Keep the same CSS layout, but expose the settling race reliably.
            pg.add_style_tag(content='.op-action,.op-rail {transition-duration:1s !important}')
        pg.locator('#op-lp-x').dispatch_event('click')
        pg.evaluate("""() => {
          const op = document.getElementById('op'); op.dataset.mode='auto'; op.dataset.busy='1';
          delete op.dataset.statusMin;
          document.getElementById('op-action-txt').textContent='Working';
          document.getElementById('op-action-sub').textContent='Reading hotel cancellation policies and comparing available rooms';
          const meter=document.getElementById('op-token-meter'); meter.hidden=false;
          document.getElementById('op-token-in').textContent='123.4K';
          document.getElementById('op-token-out').textContent='12.3K';
          const task=document.createElement('div'); task.className='op-task';
          task.dataset.busy='1';
          task.innerHTML='<div class="op-task-head">Working</div><div class="op-task-steps"><div class="op-step-count"><span class="sc-n">12 steps</span></div>' +
            Array.from({length:12}, (_,i)=>'<div class="op-task-step">Reading policy '+i+'</div>').join('')+'</div>';
          document.getElementById('op-log').append(task);
          task.querySelector('.op-step-count').append(meter);
        }""")
        layout=pg.evaluate("""() => {
          const rect=id=>document.getElementById(id).getBoundingClientRect();
          const sub=document.getElementById('op-action-sub');
          return {actionBottom:rect('op-action-sub').bottom, cardBottom:rect('op-action').bottom,
            actionHeight:rect('op-action-sub').height, tokensHeight:rect('op-token-meter').height, cardHeight:rect('op-action').height,
            actionOverflow:sub.scrollWidth>sub.clientWidth, nowrap:getComputedStyle(sub).whiteSpace,
            trace:getComputedStyle(document.querySelector('#op-log .op-task')).display,
            statusCount:document.querySelectorAll('#op-action .sc-n').length, duplicatePanel:!!document.getElementById('op-activity-panel')};
        }""")
        assert pg.locator('#op-action .op-token-meter').count() == 0
        # Zero-size boxes would satisfy the bottom-edge comparisons vacuously.
        assert min(layout['actionHeight'], layout['tokensHeight'], layout['cardHeight']) > 0, layout
        assert layout['actionBottom'] <= layout['cardBottom'], layout
        assert layout['nowrap'] == 'nowrap', layout
        assert layout['trace'] != 'none' and not layout['duplicatePanel'] and not layout['statusCount'], layout
        pg.locator('#op-action').dispatch_event('click')
        assert pg.locator('.op-task-step').count() == 12
        assert pg.locator('#op-log .op-task-step').count() == 12
        pg.locator('#op-action').dispatch_event('click')
        pg.evaluate("document.getElementById('op').dataset.busy='0'")
        # Layout transitions continue after the status/trace toggles (IN-0038).
        # Measure the settled card; timer polling also works with animation frames
        # suspended by display sleep. Keep the geometry assertions unchanged.
        pg.wait_for_function("document.getAnimations().every(a => a.playState !== 'running'"
                             " || (a.effect && a.effect.getTiming().iterations === Infinity))",
                             polling=50)
        lines = pg.locator('#op-action').evaluate('''e => {
          const card = e.getBoundingClientRect();
          return {cardHeight: card.height, rows: [...e.querySelectorAll('.op-action-sub,.op-action-meta')].map(c => {
            const r = c.getBoundingClientRect();
            return {height: r.height, bottom: r.bottom, limit: card.bottom + 1};
          })};
        }''')
        assert lines['cardHeight'] > 0 and lines['rows'], lines
        assert all(row['height'] > 0 and row['bottom'] <= row['limit'] for row in lines['rows']), lines
        pg.add_style_tag(content=':root{--fg:#e7ecf3;--muted:#7e8a9a;--border:#292b30;--border-2:#30343b;--bg-2:#14161c;--bg-3:#202125} body{background:#000;color:var(--fg)}')
        pg.screenshot(path='/tmp/operator-status-' + str(width) + '.png', full_page=True)
        assert not errors, errors
    finally:
        ctx.close()


def test_mobile_height_resize_does_not_resize_remote_browser(browser, harness):
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    ctx.add_init_script("localStorage.setItem('operator-session-v2', " + json.dumps(json.dumps(_SEEDED_SESSION)) + ");")
    page = ctx.new_page()
    page.clock.install()
    viewport_calls = []
    def steer(route):
        body = route.request.post_data_json
        if body.get('kind') == 'stage_size':
            viewport_calls.append(body)
        route.fulfill(json={'ok': True, 'applied': True})
    page.route('**/operator/steer', steer)
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.clock.run_for(1800)
        page.wait_for_function("document.cookie.includes('op_stage=')")
        assert viewport_calls, 'initial viewport report must be acknowledged'
        page.evaluate("""() => {
          window.__resizeEvents = 0;
          window.addEventListener('resize', () => window.__resizeEvents++);
        }""")
        model = page.locator('#op-model')
        assert model.evaluate('el => getComputedStyle(el).appearance') == 'none'
        assert page.locator('#op').evaluate('el => getComputedStyle(el).touchAction') == 'manipulation'
        viewport_calls.clear()
        for height in (780, 844, 790, 844):
            before = page.evaluate('window.__resizeEvents')
            page.set_viewport_size({'width': 390, 'height': height})
            page.wait_for_function('n => window.__resizeEvents > n', arg=before)
            # Cross the production 600 ms debounce after the real resize event.
            page.clock.run_for(750)
            page.evaluate('() => new Promise(requestAnimationFrame)')
        assert viewport_calls == []
        before = page.evaluate('window.__resizeEvents')
        with page.expect_response(lambda response:
                response.url.endswith('/operator/steer')
                and response.request.method == 'POST'
                and response.request.post_data_json.get('kind') == 'stage_size'):
            page.set_viewport_size({'width': 430, 'height': 844})
            page.wait_for_function('n => window.__resizeEvents > n', arg=before)
            page.clock.run_for(900)
        assert viewport_calls
    finally:
        ctx.close()


def test_completion_verification_is_not_a_stuck_warning(browser, harness):
    harness.mode = 'live'
    harness.agent_mode = 'running'
    harness.agent_messages = [
        {'ts': 9900000000, 'role': 'notice', 'kind': 'recovery', 'reason': 'verification', 'text': 'Completion check'},
        {'ts': 9900000001, 'role': 'notice', 'kind': 'recovery', 'reason': 'retry', 'text': 'Auto-replan'},
        {'ts': 9900000002, 'role': 'notice', 'kind': 'recovery', 'reason': 'loop', 'text': 'Repeated action detected'},
    ]
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    ctx.add_init_script("localStorage.setItem('operator-session-v2', " + json.dumps(json.dumps(_SEEDED_SESSION)) + ");")
    page = ctx.new_page()
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function("document.getElementById('op-log').textContent.includes('Repeated actions')")
        text = page.locator('#op-log').inner_text()
        assert 'Operator may be stuck' not in text
        assert 'Checking result' in text and 'Trying another approach' in text
        assert page.locator('.op-notice-head .warn svg').count() == 1
    finally:
        harness.agent_mode = None
        harness.agent_messages = []
        ctx.close()


@pytest.mark.parametrize("failure", ["hung-body", "bad-jpeg"])
def test_frame_failure_retries_last_painted_identity_without_overlap(browser, harness, failure):
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.add_init_script(r"""
      window.__frameRequests = []; window.__frameActive=0; window.__frameMax=0;
      const realFetch=window.fetch, realTimeout=window.setTimeout;
      window.setTimeout=(fn,ms,...args)=>realTimeout(fn,ms===8000?120:ms,...args);
      window.fetch=async function(input,options) {
        if(String(input).includes('/operator/frame')) {
          __frameRequests.push(String(input));
          if(__frameRequests.length===2 && FAILURE==='hung-body') {
            __frameActive++;__frameMax=Math.max(__frameMax,__frameActive);
            return {ok:true,status:200,headers:new Headers({'X-Operator-Frame-ID':'unpainted'}),
              blob:()=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{__frameActive--;reject(Error('aborted'));},{once:true}))};
          }
        }
        return realFetch(input,options);
      };
    """.replace("FAILURE", json.dumps(failure)))
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda error: errors.append(str(error)))
    routed = []
    def frame(route):
        routed.append(route.request.url)
        count = len(routed)
        if failure == "bad-jpeg" and count == 2:
            body, frame_id = b"not an image", "unpainted"
        else:
            body, frame_id = OV._PLACEHOLDER_JPEG, "painted-" + str(count)
        route.fulfill(status=200,body=body,headers={"Content-Type":"image/jpeg","X-Operator-Frame":"live","X-Operator-Frame-ID":frame_id})
    pg.route("**/operator/frame?*", frame)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_function("window.__frameRequests.length >= 4", timeout=6000)
        requests = pg.evaluate("__frameRequests")
        assert "since=painted-1" in requests[1]
        assert "since=painted-1" in requests[2]
        assert all("since=unpainted" not in url for url in requests)
        assert pg.evaluate("__frameMax") <= 1
        assert errors == []
    finally:
        ctx.close()


def test_gpt_speed_control_sits_before_effort_persists_and_dispatches(browser, harness):
    ctx = browser.new_context(viewport={'width': 1200, 'height': 800})
    session = dict(_SEEDED_SESSION, bot='gpt', model='gpt-6.1-sol', effort='low')
    ctx.add_init_script('localStorage.setItem("operator-session-v2", ' + json.dumps(json.dumps(session)) + ');')
    pg = ctx.new_page()
    pg.route('**/operator/models?driver=gpt', lambda r: r.fulfill(json={
        'models': [{'value': 'gpt-6.1-sol', 'label': 'GPT-6.1 Sol'}],
        'default_model': 'gpt-6.1-sol',
        'service_tiers': {'gpt-6.1-sol': ['default', 'priority', 'ultrafast']}}))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        _token_host_styles(pg)
        speed = pg.locator('#op-speed'); speed.wait_for(state='visible', timeout=8000)
        pg.select_option('#op-speed', 'ultrafast')
        pg.reload(wait_until='domcontentloaded')
        _token_host_styles(pg)
        pg.wait_for_function('document.getElementById("op-speed").value === "ultrafast"')
        bolt = pg.locator('.op-speed').bounding_box()
        effort = pg.locator('#op-effort').bounding_box()
        assert bolt and effort and bolt['x'] + bolt['width'] <= effort['x'] + 1
        assert abs((bolt['y'] + bolt['height']/2) - (effort['y'] + effort['height']/2)) < 3
        output = os.environ.get('CC_UI_QA_OUTPUT')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            pg.locator('.op-inputbox').screenshot(path=str(Path(output) / 'gpt-speed.png'))
        pg.locator('#op-input').fill('Review this page')
        pg.press('#op-input', 'Enter')
        for _ in range(40):
            if harness.dispatch_posts:
                break
            pg.wait_for_timeout(50)
        assert harness.dispatch_posts[0]['service_tier'] == 'ultrafast'
        pg.select_option('#op-action-caret', 'gemma')
        pg.wait_for_function('document.querySelector(".op-speed").hidden')
    finally:
        ctx.close()
