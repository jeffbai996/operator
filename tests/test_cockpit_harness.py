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
        self.agent_messages: list = []
        self.say_posts: list = []
        self.stop_posts: list = []
        self.dispatch_posts: list = []
        self.run_posts: list = []
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
                        "handoff": None, "surface": "browser",
                        "steer_pending": pend})
            if self.mode == "real":
                return None
            if request.path.endswith("/operator/frame"):
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
            b = p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
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
    harness.agent_messages.clear()
    harness.say_posts.clear()
    harness.stop_posts.clear()
    harness.dispatch_posts.clear()
    harness.run_posts.clear()
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


def test_boot_clean_fresh_session(page, harness):
    page.goto(harness.base + "/operator", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    assert page._errors == [], f"JS errors on fresh boot: {page._errors}"


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
        "document.querySelector('#op-model option[value=\\\"gpt-5.6-luna\\\"]')",
        polling=100)
    observed = page.evaluate("""() => {
        const model = document.getElementById('op-model');
        const effort = document.getElementById('op-effort');
        const out = {};
        for (const name of ['gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna',
                            'gpt-5.5']) {
          model.value = name;
          model.dispatchEvent(new Event('change'));
          out[name] = Array.from(effort.options, option => option.value);
        }
        return out;
    }""")
    ladder_56 = ["none", "low", "medium", "high", "xhigh", "max"]
    assert observed == {
        "gpt-5.6-sol": ladder_56,
        "gpt-5.6-terra": ladder_56,
        "gpt-5.6-luna": ladder_56,
        "gpt-5.5": ["none", "low", "medium", "high", "xhigh"],
    }


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
    assert page.locator('#op-model option[value="gpt-5.6-luna"]').count() == 1
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


def test_midrun_message_interrupt_steers(browser, harness):
    """Interrupt-steer (restored 2026-07-12, the owner): a message sent while a run
    is LIVE STOPS the current turn and immediately re-dispatches with the new
    text — barge-in, not the 1.0.12 soft-steer queue. So a mid-run message must
    POST /operator/agent/stop then /operator/dispatch, and must NOT POST
    /operator/agent/say. The user bubble renders exactly once."""
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
        pg.wait_for_timeout(2500)   # stop → 350ms settle → re-dispatch
        assert harness.stop_posts, "interrupt-steer must STOP the live run"
        assert len(harness.dispatch_posts) == 1, "interrupt-steer must re-dispatch once"
        assert harness.dispatch_posts[0].get("task") == "switch to the CAD listing", \
            "interrupt-steer must re-dispatch the new text"
        assert harness.say_posts == [], "interrupt-steer must NOT soft-queue via say"
        assert pg.locator("#op-log .op-msg.user").count() == 1
        assert errors == [], f"JS errors during steer: {errors}"
    finally:
        harness.agent_mode = None
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


def test_var_task_card_prefills_composer(browser, harness):
    """1.0.13: clicking Go on a {{variable}} saved task loads the prompt into
    the composer (first placeholder selected) and fires NOTHING — no task run,
    no dispatch (the server would 400 an unfilled template anyway)."""
    import operator_tasks as OT
    slug, err = OT.save_task({"name": "Price check",
                              "prompt": "find the price of {{item}} on {{site}}"})
    assert err is None
    harness.run_posts.clear()
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
        assert metrics["version"] == "1.1.0"
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
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="visible", timeout=8000)
        pg.wait_for_function(
            "document.getElementById('op-lp-x')._wired === true",
            timeout=8000, polling=50)
        pg.dispatch_event("#op-lp-x", "click")
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
        model="gpt-5.6-sol", effort="low", surface="browser", demo=False,
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
        assert body["model"] == "gpt-5.6-sol"
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


def _restored_ctx(browser, **ctx_kw):
    """Context with a believable RESTORED session: nonempty chat, auto mode
    (auto is the mode that keeps the splash CSS-visible — the iPad state)."""
    ctx = browser.new_context(**ctx_kw)
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v2', "
        + json.dumps(json.dumps(_SEEDED_SESSION)) + ");")
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


def test_trash_clear_returns_to_opaque_splash(browser, harness):
    """Trashing a conversation lands on the SOLID splash, not the translucent
    over-the-feed blur (the owner 2026-07-18, superseding the 07-17 blur note)."""
    ctx = _restored_ctx(browser, viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    errors, con_errors, _ = _collectors(pg)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=4000)
        pg.dispatch_event("#op-clear", "click")
        pg.wait_for_selector("#op-lp", state="visible", timeout=4000)
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
        " padding: 0.86rem 3rem 0.98rem 0.92rem !important; }"
        " .op-lp-input { font-size: 16px !important; width: 142.857% !important;"
        " transform: scale(.7) !important; transform-origin: left top !important; }")
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
    finally:
        ctx.close()
