"""Client-side cockpit harness — loads the REAL operator page in headless
Chromium and asserts the JS layer behaves, which server-side tests cannot see
(the 2026-06-26 feed-death post-mortem: a TDZ init crash killed every feature
while every server test stayed green).

What this covers:
  * boot with a fresh AND a seeded `operator-session-v2` produces zero
    `pageerror` events (the TDZ-crash class),
  * placeholder frames are NOT treated as live signal — with the backend in
    the exact 2026-07-10 production failure state (HTTP 200 placeholder
    frames + status "error") the cockpit settles into SIGNAL LOST and stays
    there, no Connecting↔Reconnecting word flap, no class strobing,
  * on signal drop after real frames the stage freezes the last frame
    (op-signal-stale, no full overlay) and recovers cleanly when the feed
    returns.

Run under the host-app venv (the one that owns playwright — also the venv
that actually serves this page in production):

  cd modules/operator && PYTHONPATH=. \
    ../host-app/venv/bin/python -m pytest tests/test_cockpit_harness.py -q

Under the repo-root venv (no playwright) the whole module skips loudly.

The streamer here is pointed at a DEAD CDP port before operator_view is
(re)loaded — it can never touch the real logged-in Chrome on :9222.
"""
import json
import importlib
import os
import threading

import pytest

pw_sync = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright not in this venv — run under modules/host-app/venv")

from flask import Flask, Response, jsonify, request  # noqa: E402
from jinja2 import ChoiceLoader, DictLoader          # noqa: E402
from werkzeug.serving import make_server             # noqa: E402

# Must be set BEFORE operator_view is (re)loaded: CDP_URL is read at import
# time. A dead loopback port → every attach fails fast with ECONNREFUSED and
# the harness can never reach the real browser.
_DEAD_CDP = "http://127.0.0.1:9299"
# Collection imports this module before ANY test runs, so these writes are
# visible to every other module; _restore_harness_env puts them back once the
# harness is done (2026-09-22: six Astra launch tests failed in a full run
# because they saw this dead endpoint and built a demo launch plan).
_PRIOR_ENV = {k: os.environ.get(k) for k in (
    "OPERATOR_DEMO_CDP", "OPERATOR_DEMO", "OPERATOR_CHROME_LAUNCHER",
    "OPERATOR_SESSION_PATH")}
os.environ["OPERATOR_DEMO_CDP"] = _DEAD_CDP
os.environ.pop("OPERATOR_DEMO", None)   # live cockpit template, not the demo
# Demand-start must fail locally too. A dead CDP endpoint alone stopped being
# sufficient once the production streamer learned to launch Chrome on demand;
# without this override the harness can invoke the real :9222 launcher.
os.environ["OPERATOR_CHROME_LAUNCHER"] = "/nonexistent/operator-harness-launcher"
# isolate the shared-session store — harness pages sync the session on boot
# and must NEVER read or pollute the real cockpit's session file
import tempfile  # noqa: E402
_HARNESS_STATE_DIR = tempfile.mkdtemp(prefix="op-harness-state-")
os.environ["OPERATOR_SESSION_PATH"] = os.path.join(
    _HARNESS_STATE_DIR, "session.json")

import operator_session as OS_MOD  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _restore_harness_env():
    yield
    for key, value in _PRIOR_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
import operator_view as OV  # noqa: E402
importlib.reload(OS_MOD)   # rebind the store path under the isolated env

# same stand-in the route characterization tests use — the real _base.html
# belongs to the parent host-app app; operator.html only fills its
# `title` and `content` blocks.
_STUB_BASE = ("<!doctype html><title>{% block title %}{% endblock %}</title>"
              # render the favicon block: without a rendered icon link Chromium
              # requests /favicon.ico, the harness 404s it, and every
              # zero-console-error assertion fails (started with the real
              # _base.html gaining a favicon block the stub lacked)
              "{% block favicon %}{% endblock %}"
              "<style>button{padding:.4rem .7rem;display:inline-flex;gap:.4rem}</style>"
              "<div class=\"wrap\"><header class=\"site\" id=\"test-site-header\">site nav</header>"
              "<main>{% block content %}{% endblock %}</main></div>")

# status JSON in the exact shape /operator/status emits for the browser surface
_STATUS_LIVE = {"status": "live", "detail": "", "has_frame": True,
                "vw": 1280, "vh": 800, "url": "https://example.com",
                "click": None, "surface": "browser"}
_STATUS_DEAD = {"status": "error", "detail": "disconnected", "has_frame": False,
                "vw": 0, "vh": 0, "url": "", "click": None, "surface": "browser"}


class _Harness:
    """Ephemeral server wrapper: real blueprint + a mode switch the tests flip.

    mode 'real' — requests hit the actual routes (dead CDP ⇒ the server serves
                  200 placeholder frames + status 'error': the 2026-07-10
                  production failure state, verbatim).
    mode 'live' — fake healthy feed: real JPEG bytes stamped live + status live.
    mode 'dead' — hard down: /frame 503 + status error (frames stop entirely).
    """

    def __init__(self) -> None:
        self.mod = importlib.reload(OV)
        assert self.mod.CDP_URL == _DEAD_CDP, "harness must never see real CDP"
        self.mode = "real"
        # agent_mode "running" fakes a live agent run (1.0.12 steer tests):
        # /operator/agent reports state=running, say/stop/dispatch POSTs are
        # recorded instead of reaching the real runner.
        self.agent_mode = None
        self.agent_handoff = None
        self.agent_messages: list = []
        self.say_posts: list = []
        self.stop_posts: list = []
        self.dispatch_posts: list = []
        self.run_posts: list = []
        self.allowed_task_slug = None
        self.frame_tiers: list[str] = []
        self._steer_pending = 0
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.register_blueprint(self.mod.bp)
        app.jinja_loader = ChoiceLoader([app.jinja_loader,
                                         DictLoader({"_base.html": _STUB_BASE})])

        @app.before_request
        def _mode_gate():  # noqa: ANN202
            # NO test may ever start a real agent run — regardless of mode
            if request.path.endswith("/operator/dispatch"):
                self.dispatch_posts.append(request.get_json(silent=True) or {})
                return Response("harness: dispatch blocked", status=403)
            if (request.path.startswith("/operator/tasks/")
                    and request.path.endswith("/run")):
                self.run_posts.append(request.path)
                if request.path != f"/operator/tasks/{self.allowed_task_slug}/run":
                    return Response("harness: task run blocked", status=403)
            # Cockpit tests do not exercise the remote browser tab inventory.
            # Short-circuit it so a failed/dead synthetic CDP loop cannot leave
            # run_coroutine_threadsafe futures pending during page teardown.
            if request.path.endswith("/operator/tabs"):
                return jsonify(tabs=[])
            if self.agent_mode == "running":
                import time as _t
                if request.path.endswith("/operator/agent/say"):
                    txt = (request.get_json(silent=True) or {}).get("text", "")
                    self.say_posts.append(txt)
                    self._steer_pending = 1
                    return jsonify(ok=True, queued=1, live=True)
                if request.path.endswith("/operator/agent/stop"):
                    self.stop_posts.append(1)
                    return jsonify(ok=True)
                if request.path.endswith("/operator/agent"):
                    # serve the queued count once, then report it consumed —
                    # the client should log the "Steer delivered" notice. The
                    # echoed role=user message must NOT re-render client-side.
                    pend, self._steer_pending = self._steer_pending, 0
                    msgs = ([{"ts": _t.time(), "role": "user", "text": t}
                             for t in self.say_posts] + self.agent_messages)
                    return jsonify({
                        "bot": "claude-a", "task": "long research task",
                        "state": "running", "started_ts": _t.time() - 30,
                        "ended_ts": 0, "messages": msgs, "final": "",
                        "alive": True, "stalled": False, "stalled_for": 0,
                        "handoff": self.agent_handoff, "surface": "browser",
                        "steer_pending": pend})
            if self.mode == "real":
                return None
            if request.path.endswith("/operator/frame"):
                self.frame_tiers.append(request.args.get("tier", ""))
                if self.mode == "dead":
                    return Response("down", status=503)
                resp = Response(self.mod._PLACEHOLDER_JPEG, mimetype="image/jpeg")
                resp.headers["X-Operator-Frame"] = "live"
                resp.headers["Cache-Control"] = "no-store"
                return resp
            if request.path.endswith("/operator/status"):
                return jsonify(_STATUS_LIVE if self.mode == "live"
                               else _STATUS_DEAD)
            return None

        self.app = app
        self._srv = make_server("127.0.0.1", 0, app, threaded=True)
        self.base = f"http://127.0.0.1:{self._srv.server_port}"
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        daemon=True, name="cockpit-harness")
        self._thread.start()

    def stop(self) -> None:
        try:
            self._srv.shutdown()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(scope="module")
def harness():
    h = _Harness()
    yield h
    h.stop()


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True,
                executable_path=os.environ.get("OPERATOR_TEST_CHROMIUM") or None)
        except Exception as e:  # noqa: BLE001
            if os.environ.get("OPERATOR_REQUIRE_BROWSER") == "1":
                pytest.fail(f"required headless Chromium unavailable: {e}")
            pytest.skip(f"headless chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture(autouse=True)
def _fresh_session_store(monkeypatch, harness):
    """Each test gets an empty shared-session store — otherwise a session
    pushed by an earlier test's page boot gets ADOPTED by the next test's
    fresh context (log swap + mode re-apply mid-test = flaky sampling)."""
    harness.mode = "real"
    harness.agent_mode = None
    harness.agent_handoff = None
    harness.agent_messages.clear()
    harness.say_posts.clear()
    harness.stop_posts.clear()
    harness.dispatch_posts.clear()
    harness.run_posts.clear()
    harness.allowed_task_slug = None
    harness.frame_tiers.clear()
    session_path = os.path.join(_HARNESS_STATE_DIR, "session.json")
    # Other test modules reload operator_session against their own tmp paths.
    # Rebind its module-level path here as well as restoring the environment;
    # unlinking only the env path leaves the already-imported store pointed at
    # the previous module's file when the suites run in one pytest process.
    monkeypatch.setenv("OPERATOR_SESSION_PATH", session_path)
    import operator_session as _osess
    importlib.reload(_osess)
    try:
        os.unlink(session_path)
    except FileNotFoundError:
        pass
    # pagehide uses a final asynchronous session POST. Chromium can finish
    # that local request just after the previous context closes; give it one
    # short drain window, then clear again before this page is allowed to boot.
    threading.Event().wait(0.05)
    try:
        os.unlink(session_path)
    except FileNotFoundError:
        pass
    # The conversation registry instantiates runners lazily. Keep the harness
    # on a blank state file too: otherwise /operator/agent can hydrate messages
    # from the live cockpit and hide the launchpad halfway through an assertion.
    # Scope both the env and registry to this test so collection does not leak
    # OPERATOR_STATE_PATH into prompt/state-machine tests elsewhere in the suite.
    state_path = os.path.join(_HARNESS_STATE_DIR, "operator-state.json")
    try:
        os.unlink(state_path)
    except FileNotFoundError:
        pass
    monkeypatch.setenv("OPERATOR_STATE_PATH", state_path)
    monkeypatch.setattr(
        OV.operator_agent, "runner", OV.operator_agent.RunnerRegistry())
    with _osess._PRESENCE_LOCK:
        _osess._PRESENCE.clear()
    yield
    # Context teardown can finish one last session flush after the test body.
    # Clear at both boundaries so that write cannot seed the next test.
    for path in (session_path, state_path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


@pytest.fixture()
def page(browser, harness):
    """Fresh context per test, pageerror collector attached, mode reset."""
    harness.mode = "real"
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.bring_to_front()
    pg._errors = []
    pg.on("pageerror", lambda e: pg._errors.append(str(e)))
    yield pg
    ctx.close()


# a believable restored session: chat log with user/bot bubbles, a copy button
# and a handoff card (restoreSession strips + rebuilds both), auto mode.
_SEEDED_LOG = (
    '<div class="op-msg user"><div class="bubble">find me a flight to tokyo'
    '</div></div>'
    '<div class="op-msg bot"><div class="bubble">on it — checking fares'
    '<button class="op-copy">copy</button></div></div>'
    '<div class="op-handoff">agent asks you to take the wheel</div>'
)
_SEEDED_SESSION = {"log": _SEEDED_LOG, "mode": "auto",
                   "bot": "", "model": "", "effort": ""}


def _sample_signal_state(pg, samples: int = 20, every_ms: int = 150) -> list:
    """In-page sampler: card word + signal classes, one evaluate round-trip."""
    return pg.evaluate(
        """([n, ms]) => new Promise(res => {
             const op = document.getElementById('op');
             const t = document.getElementById('op-action-txt');
             const out = [];
             const iv = setInterval(() => {
               out.push({txt: (t && t.textContent || '').trim(),
                         stale: op.classList.contains('op-signal-stale'),
                         lost: op.classList.contains('op-signal-lost')});
               if (out.length >= n) { clearInterval(iv); res(out); }
             }, ms);
           })""",
        [samples, every_ms])


def _transitions(values: list) -> int:
    return sum(1 for a, b in zip(values, values[1:]) if a != b)


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


def test_boot_clean_fresh_session(page, harness):
    page.goto(harness.base + "/operator", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    assert page._errors == [], f"JS errors on fresh boot: {page._errors}"


def _connection_context(browser, *, width=1280, connection=None,
                        slow_body_ms=0):
    ctx = browser.new_context(viewport={"width": width, "height": 820})
    payload = json.dumps([connection or {}, slow_body_ms])
    ctx.add_init_script("""(() => {
      const [connection, slowMs] = %s;
      Object.defineProperty(navigator, 'connection', {
        configurable: true, value: Object.assign({
          type: 'wifi', effectiveType: '4g', saveData: false
        }, connection || {})
      });
      window.__opSlowBodyMs = slowMs;
      if (slowMs) {
        const nativeBlob = Response.prototype.blob;
        Response.prototype.blob = async function() {
          const delay = window.__opSlowBodyMs || 0;
          if (delay && this.url.includes('/operator/frame'))
            await new Promise(resolve => setTimeout(resolve, delay));
          return nativeBlob.call(this);
        };
      }
    })()""" % payload)
    return ctx


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
        assert "eco" in harness.frame_tiers
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
        assert "lo" in harness.frame_tiers
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
        assert tier in harness.frame_tiers
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
        for (const name of ['gpt-6-astra', 'gpt-6-sol', 'gpt-5.6-terra',
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
        "gpt-6-sol": delegated,
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
    assert page.locator('#op-model option[value="claude-sonnet-5"]').count() == 0
    assert page._errors == [], f"JS errors: {page._errors}"


def test_placeholder_frames_not_treated_as_signal(page, harness):
    """Backend in the 2026-07-10 failure state: /frame serves HTTP 200
    PLACEHOLDER frames while /status reports error. Placeholders must not
    count as signal: the cockpit settles into SIGNAL LOST and holds it —
    no Connecting↔Reconnecting word flap, no stale/lost class strobing."""
    harness.mode = "real"
    page.goto(harness.base + "/operator", wait_until="domcontentloaded")
    # give it two status polls (1.5s cadence) to reach the lost state
    page.wait_for_function(
        "document.getElementById('op').classList.contains('op-signal-lost')"
        " || document.getElementById('op').classList.contains('op-signal-stale')",
        timeout=8000, polling=100)
    page.wait_for_timeout(1500)          # let any flap start flapping
    samples = _sample_signal_state(page)  # 3s steady window
    words = [s["txt"] for s in samples]
    classes = [(s["stale"], s["lost"]) for s in samples]
    assert _transitions(words) <= 1, f"status word flaps: {words}"
    assert _transitions(classes) <= 1, f"signal classes strobe: {classes}"
    # placeholders never became "signal": full SIGNAL LOST overlay, feed hidden
    last = samples[-1]
    assert last["lost"] and not last["stale"], f"expected lost overlay: {last}"
    assert page.eval_on_selector("#op-overlay-text",
                                 "el => el.textContent") == "SIGNAL LOST"
    assert page.eval_on_selector("#op-view",
                                 "el => el.style.visibility") == "hidden"
    assert page._errors == [], f"JS errors: {page._errors}"


def test_stale_freeze_and_recovery(page, harness):
    """Live feed → signal drop → the stage FREEZES the last real frame
    (op-signal-stale; no full-screen overlay; feed stays visible) with a
    stable 'Reconnecting' card — then recovers to Ready when frames return."""
    harness.mode = "live"
    page.goto(harness.base + "/operator", wait_until="domcontentloaded")
    page.wait_for_function(
        "document.getElementById('op').dataset.state === 'live'",
        timeout=8000, polling=100)
    page.wait_for_timeout(500)
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


# ------------------------------------------- one shared server session -----

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


def test_connector_action_uses_a_provider_favicon(page, harness):
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
    try:
        page.goto(harness.base + "/operator", wait_until="domcontentloaded")
        # The harness keeps an off-screen trace clone for its responsive shell;
        # assert against the newest real row without turning that implementation
        # detail into a visibility requirement.
        row = page.locator(".op-connector-step").last
        row.wait_for(state="attached", timeout=8000)
        # Responsive layout may place the detail on its own visual line; the
        # semantic label is unchanged, so compare normalized rendered text.
        assert " ".join(row.inner_text().split()) == \
            "Using Booking.com · Searching accommodations"
        favicon = row.locator(".op-connector-favicon")
        assert favicon.count() == 1
        assert "booking.com" in (favicon.get_attribute("src") or "")
        assert "booking_com.accommodations_search_v2" not in row.inner_text()
        assert page._errors == [], f"JS errors during connector render: {page._errors}"
    finally:
        harness.agent_mode = None
        harness.agent_messages = []


def test_agent_action_label_is_rendered_as_text_in_status(browser, harness):
    """Agent trace labels must stay text when copied into the live subline."""
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
            "document.getElementById('op-action-sub').textContent"
            ".includes('agent-action-xss')",
            timeout=8000,
            polling=50,
        )

        sub = pg.locator("#op-action-sub")
        assert pg.locator("#agent-action-xss").count() == 0
        assert pg.locator("body").get_attribute("id") != "agent-action-xss-fired"
        assert sub.locator(":scope > .sub-bot").text_content() == "claude-a"
        assert sub.locator(":scope > .sub-emo").text_content() == "⚙️"
        assert sub.text_content() == f"claude-a · {hostile_verb.lower()} ⚙️"

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
        # showModal is a top-layer overlay, never a launchpad flex child.
        page.evaluate("document.getElementById('op-files-open').click()")
        page.wait_for_selector('#op-files-dialog[open]')
        bounds = page.locator('#op-files-dialog').bounding_box()
        assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 391
        text = page.locator('#op-files-dialog').inner_text()
        # The add control is the drop zone; the footer is a bare meter, not a sentence.
        assert 'Add files' in text and 'per file' in text
        assert 'drop it' not in text and 'Files stay' not in text
        page.get_by_role('button', name='Close', exact=True).last.click()
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
        if page.locator('#op-lp-x').is_visible():
            page.locator('#op-lp-x').click()
        page.wait_for_selector('.op-wb-approval', state='visible')
        page.evaluate('window.OperatorWorkbench.toggleJob()')
        assert 'CAD 60.00' in page.locator('.op-wb-approval').inner_text()
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


def test_mobile_attachment_and_send_share_center_at_all_composer_sizes(browser, harness):
    harness.mode = 'live'
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    page = ctx.new_page()
    try:
        page.goto(harness.base + '/operator', wait_until='domcontentloaded')
        page.wait_for_function('!!window.OperatorWorkbench')
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
        assert card.locator('table tbody tr').count() == 1
        assert card.locator('pre code').inner_text() == 'print(288)'
        assert card.locator('.op-copy').count() == 1
        assert card.locator('img').count() == 0
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
        if page.locator('#op-lp-x').is_visible(): page.locator('#op-lp-x').click()
        card.scroll_into_view_if_needed()
        assert card.evaluate('el => el.scrollWidth <= el.clientWidth')
        page.screenshot(path='/tmp/operator-rich-result.png')
    finally:
        ws.finish_run(run['run_id'], 'done'); ctx.close()


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


def _expand_launchpad(pg):
    """The splash boots COLLAPSED — since 1.0.26 the class ships in the markup
    itself (the old post-paint JS collapse flashed the tabs/grid on every
    refresh). Tests that assert expanded-state behavior opt in the way a user
    does: open the Browse category."""
    pg.bring_to_front()
    pg.wait_for_selector("#op-lp-wordmark", state="visible", timeout=8000)
    pg.locator('.op-lp-cat[data-category="all"]').dispatch_event("click")
    pg.wait_for_selector(".op-lp-card", state="visible", timeout=8000)
    pg.wait_for_timeout(500)   # grid crossfade + gap transition settle


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

    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.bring_to_front()
        pg.wait_for_selector("#op-lp-open", state="visible", timeout=8000)
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
        # 3-stop cycle: dark → OLED flat (data-theme untouched) → light → dark
        pg.locator("#op-lp-theme").dispatch_event("click")
        assert pg.locator("html").get_attribute("data-theme") == "dark"
        assert "op-flat" in pg.locator("#op").get_attribute("class")
        pg.locator("#op-lp-theme").dispatch_event("click")
        assert pg.locator("html").get_attribute("data-theme") == "light"
        assert pg.evaluate("localStorage.getItem('squad_theme')") == "light"
        assert "op-flat" not in pg.locator("#op").get_attribute("class")
        pg.locator("#op-lp-theme").dispatch_event("click")
        assert pg.locator("html").get_attribute("data-theme") == "dark"
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
        assert metrics["version"] == "1.2.1"
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
        stage = pg.locator("#op-stage").bounding_box()
        assert stage is not None

        # Chromium can wait indefinitely for a compositor frame while
        # Playwright's touchscreen.tap drives a headless mobile context. Send
        # the same DOM touch sequence directly: this test owns the touch
        # handler/focus contract, not Chromium's input-device transport.
        pg.evaluate("""([x, y]) => {
          const el = document.getElementById('op-stage');
          const touch = new Touch({identifier:1, target:el, clientX:x, clientY:y});
          el.dispatchEvent(new TouchEvent('touchstart', {
            bubbles:true, cancelable:true, touches:[touch], targetTouches:[touch],
            changedTouches:[touch]}));
          el.dispatchEvent(new TouchEvent('touchend', {
            bubbles:true, cancelable:true, touches:[], targetTouches:[],
            changedTouches:[touch]}));
        }""", [stage["x"] + stage["width"] / 2,
                 stage["y"] + stage["height"] / 2])
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
    """The minimized status pill is absolute, so the manual card must supply
    only its overlap clearance—not the old expanded-card-sized void."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        # A previous harness page can legitimately leave the isolated shared
        # session in its collapsed launchpad state. This test sets the exact
        # minimized-status geometry below, so require the shell to exist rather
        # than accidentally coupling the assertion to suite execution order.
        pg.wait_for_selector("#op-lp", state="attached", timeout=8000)
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


def test_history_run_again_redispatches_row_bundle(browser, harness):
    """1.0.13: ↻ on a History row re-dispatches with the ROW's bot/model/
    effort/surface — not the current pickers."""
    import time as _t
    import types
    import operator_history as OH
    rid = OH.record(types.SimpleNamespace(
        bot="gpt", task="scan the weekly filings", state="done",
        model="gpt-6-sol", effort="low", surface="browser", demo=False,
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
        assert body["model"] == "gpt-6-sol"
        assert body["effort"] == "low"
        assert body["surface"] == "browser"
        assert errors == [], f"JS errors: {errors}"
    finally:
        ctx.close()


# ── restored-session launchpad wiring (the 2026-07-18 real-iPad lockout) ────
# The initializer used to bail on `if (log.children.length) return` BEFORE any
# control wiring. A device with a cached nonempty session therefore painted
# the splash (op-booting shows it, nothing ever set [hidden]) with ZERO live
# listeners — cards, category pills, X, HOME, theme, composer all dead — and
# never issued the /operator/tasks fetch that marks a completed init. Desktop
# escaped only because a cached mode of 'man' CSS-hides the splash outright.
# These contracts pin the split: wiring always runs; visibility is a separate
# decision; a restored log hides the splash but never disarms it.


def _seed_once_script(session: dict) -> str:
    """Seed localStorage on the FIRST navigation of the tab only. An init
    script runs on every navigation, so a plain setItem re-seeds the old
    chat after the clear button's reload and the test boots back into the
    conversation it just deleted (2026-09-22: two harness tests failed for
    two months on exactly this)."""
    return ("if (!sessionStorage.getItem('op-harness-seeded')) {"
            " localStorage.setItem('operator-session-v2', "
            + json.dumps(json.dumps(session)) + ");"
            " sessionStorage.setItem('op-harness-seeded', '1'); }")


_RELOADED = "() => performance.getEntriesByType('navigation').some(e => e.type === 'reload')"


def _restored_ctx(browser, **ctx_kw):
    """Context with a believable RESTORED session: nonempty chat, auto mode
    (auto is the mode that keeps the splash CSS-visible — the iPad state)."""
    ctx = browser.new_context(**ctx_kw)
    ctx.add_init_script(_seed_once_script(_SEEDED_SESSION))
    return ctx


def _collectors(pg):
    """pageerror + console-error + /operator/tasks request recorders."""
    errors, con_errors, tasks_reqs = [], [], []
    pg.on("pageerror", lambda e: errors.append(str(e)))

    def _console(m):
        if m.type != "error":
            return
        # OFF-ORIGIN resource failures are environment noise, not app bugs:
        # saved-task cards fetch per-site favicons from Google's service, and
        # any site gstatic has no icon for (e.g. nih.gov in the live task
        # store) 404s in the console — the assertion is about OUR code.
        loc = (m.location or {}).get("url", "")
        if "Failed to load resource" in m.text and loc and "127.0.0.1" not in loc:
            return
        con_errors.append(m.text)

    pg.on("console", _console)
    pg.on("request",
          lambda r: tasks_reqs.append(r.url)
          if r.url.split("?")[0].rstrip("/").endswith("/operator/tasks")
          else None)
    return errors, con_errors, tasks_reqs


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


@pytest.mark.parametrize('width', [390, 1440])
def test_launchpad_connection_ring_tracks_demand_start(browser, harness, width):
    """A down CDP probe while Chrome starts is yellow, not a red failure.

    Real page/CSS, synthetic status and dead CDP: no Chrome or model work.
    """
    harness.mode = 'live'
    status = dict(_STATUS_DEAD, status='connecting', browser_up=False,
                  detail='starting browser…')
    ctx = browser.new_context(viewport={'width': width, 'height': 900})
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda error: errors.append(str(error)))
    pg.route('**/operator/status', lambda route: route.fulfill(json=status))
    try:
        pg.goto(harness.base + '/operator', wait_until='domcontentloaded')
        pg.add_style_tag(content=':root { --bad: #f85149; --fg: #ffffff; }')
        pg.wait_for_selector('#op.op-browser-down[data-connection="connecting"]')
        # Cross both the old 4s branding cutoff and the 6s cold-feed overlay.
        pg.wait_for_timeout(6600)

        def appearance():
            return pg.evaluate("""() => ({
              ring: getComputedStyle(document.querySelector('.op-lp-mark-sweep')).stroke,
              glyph: getComputedStyle(document.querySelector('.op-lp-mark-glyph')).stroke,
              animation: getComputedStyle(document.querySelector('.op-lp-mark-glyph')).animationName,
              copy: getComputedStyle(document.querySelector('.op-lp-mark-tip'), '::before').content,
              label: document.getElementById('op-lp-mark').getAttribute('aria-label')
            })""")

        pending = appearance()
        assert pending['ring'] == 'rgb(231, 185, 62)'
        assert pending['glyph'] == 'rgb(245, 245, 245)'
        assert pending['animation'] == 'op-mark-glyph-turn'
        assert pending['copy'] == '"Connecting…"'
        assert pending['label'] == 'Connecting…'
        pg.emulate_media(reduced_motion='reduce')
        assert appearance()['animation'] == 'none'
        pg.emulate_media(reduced_motion='no-preference')

        # Backend reports the actual launcher failure/timeout.
        status.update(status='error', detail='browser could not start: timed out')
        pg.wait_for_selector('#op[data-connection="error"]')
        assert appearance()['ring'] == 'rgb(248, 81, 73)'
        assert appearance()['copy'] == '"Connection error"'

        # A retry must spin again even though the boot animation already settled.
        status.update(status='connecting')
        pg.wait_for_selector('#op[data-connection="connecting"]')
        assert appearance()['animation'] == 'op-mark-glyph-turn'

        # An attached frame outranks the briefly cached negative CDP probe.
        status.update(_STATUS_LIVE)
        pg.wait_for_selector('#op[data-connection="live"]')
        assert appearance()['ring'] == 'rgb(63, 185, 80)'
        assert appearance()['copy'] == '"Connected"'
        assert appearance()['animation'] != 'op-mark-glyph-turn'
        assert not errors, errors
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


# ── 2026-07-18 evening polish: trash presentation + iOS composer geometry ───


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
        pg.wait_for_timeout(1200)
        assert pg.locator('#op-log').inner_text() == '', pg.locator('#op-log').inner_html()
        pg.wait_for_selector('#op-lp', state='visible', timeout=8000)
        replacement = pg.evaluate("sessionStorage.getItem('operator-conversation-v2')")
        assert replacement != sid
        with pytest.raises(KeyError):
            OS_MOD.load(sid)
        pg.dispatch_event('#op-lp-x', 'click')
        pg.wait_for_timeout(1800)
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
        assert color == 'rgb(120, 186, 255)'
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


def test_chat_library_hides_delegations_until_asked(browser, harness):
    """Bot-made delegation chats (origin mcp) stay out of the human's library
    by default; the Delegations filter shows exactly them (2026-09-22)."""
    mine = OS_MOD.create()["id"]
    OS_MOD.save({"log": '<div class="op-msg user"><span class="bubble">mine</span></div>',
                 "preview": "Book the ferry", "bot": "gpt", "surface": "browser"},
                conversation_id=mine)
    OS_MOD.title_if_unset("Ferry", mine)
    bot = OS_MOD.create("Compare rates", activate=False, reuse_draft=False, origin="mcp")["id"]
    harness.mode = "live"
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp-open", state="visible", timeout=8000)
        pg.locator("#op-lp-open").dispatch_event("click")
        pg.wait_for_selector("#op-lp-chats", state="visible", timeout=8000)
        pg.locator("#op-lp-chats").dispatch_event("click")
        pg.wait_for_selector(".op-chat-row", state="visible", timeout=8000)
        assert pg.locator(".op-chat-title").all_text_contents() == ["Ferry"]
        pg.select_option("#op-chat-filter", "delegations")
        pg.wait_for_timeout(300)
        assert pg.locator(".op-chat-title").all_text_contents() == ["Compare rates"]
        pg.select_option("#op-chat-filter", "all")
        pg.wait_for_timeout(300)
        assert pg.locator(".op-chat-title").all_text_contents() == ["Ferry"]
        assert errors == []
    finally:
        ctx.close()
        for sid in (mine, bot):
            try:
                OS_MOD.delete(sid)
            except KeyError:
                pass
