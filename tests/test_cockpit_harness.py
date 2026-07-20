"""Client-side cockpit harness — loads the REAL operator page in headless
Chromium and asserts the JS layer behaves, which server-side tests cannot see
(the 2026-06-26 feed-death post-mortem: a TDZ init crash killed every feature
while every server test stayed green).

What this covers:
  * boot with a fresh AND a seeded `operator-session-v1` produces zero
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
# isolate the shared-session store — harness pages sync the session on boot
# and must NEVER read or pollute the real cockpit's session file
import tempfile  # noqa: E402
os.environ["OPERATOR_SESSION_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="op-harness-sess-"), "session.json")

import operator_session as OS_MOD  # noqa: E402
import operator_view as OV  # noqa: E402
importlib.reload(OS_MOD)   # rebind the store path under the isolated env

# same stand-in the route characterization tests use — the real _base.html
# belongs to the parent host-app app; operator.html only fills its
# `title` and `content` blocks.
_STUB_BASE = ("<!doctype html><title>{% block title %}{% endblock %}</title>"
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
                             for t in self.say_posts])
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
def _fresh_session_store():
    """Each test gets an empty shared-session store — otherwise a session
    pushed by an earlier test's page boot gets ADOPTED by the next test's
    fresh context (log swap + mode re-apply mid-test = flaky sampling)."""
    try:
        os.unlink(os.environ["OPERATOR_SESSION_PATH"])
    except FileNotFoundError:
        pass
    yield


@pytest.fixture()
def page(browser, harness):
    """Fresh context per test, pageerror collector attached, mode reset."""
    harness.mode = "real"
    ctx = browser.new_context()
    pg = ctx.new_page()
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
        "localStorage.setItem('operator-session-v1', "
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
        timeout=8000)
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
        "document.getElementById('op').dataset.state === 'live'", timeout=8000)
    page.wait_for_timeout(500)
    op_classes = page.eval_on_selector("#op", "el => el.className")
    assert "op-signal" not in op_classes, f"live but signal class set: {op_classes}"

    harness.mode = "dead"
    page.wait_for_function(
        "document.getElementById('op').classList.contains('op-signal-stale')",
        timeout=8000)
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
        timeout=8000)
    page.wait_for_function(
        "document.getElementById('op-action-txt').textContent.trim() === 'Ready'",
        timeout=8000)
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
            timeout=6000)
        assert errors == [], f"JS errors adopting server session: {errors}"
    finally:
        ctx.close()


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
        pg.wait_for_timeout(800)
        pg.click("#op-mode .op-mode-btn[data-mode='auto']")
        pg.wait_for_timeout(1800)        # debounce (600ms) + round-trip slack
        after = _json.loads(urllib.request.urlopen(
            harness.base + "/operator/session").read())
        assert after["rev"] > before, "mode toggle must push a new session rev"
        assert after["data"]["mode"] == "auto"
    finally:
        ctx.close()


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
        "localStorage.setItem('operator-session-v1', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        # the agent poll marks the run in-flight → the send button flips to ■
        pg.wait_for_function(
            "document.getElementById('op-send').classList.contains('stopping')",
            timeout=8000)
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
        "localStorage.setItem('operator-session-v1', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector(".op-lp-card", timeout=8000)
        pg.click("#op-lp-tasks-toggle")
        pg.wait_for_selector(".op-lp-card", timeout=8000)
        pg.hover(".op-lp-card")          # Go is hover-revealed on desktop
        pg.click(".op-lp-card .op-lp-go")
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
        "localStorage.setItem('operator-session-v1', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp-input", state="visible", timeout=8000)
        assert pg.locator("#op-lp-wordmark").text_content() == "Operator"
        hero = pg.locator("#op-lp-input")
        hero.click()
        pg.keyboard.type("Find two quiet hotels near Union Square")
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
        "localStorage.setItem('operator-session-v1', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp-input", state="visible", timeout=8000)
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
    pg.wait_for_selector("#op-lp-wordmark", state="visible", timeout=8000)
    pg.click('.op-lp-cat[data-category="all"]')
    pg.wait_for_selector(".op-lp-card", state="visible", timeout=8000)
    pg.wait_for_timeout(500)   # grid crossfade + gap transition settle


def test_launchpad_wordmark_is_centered_jakarta_hero(browser, harness):
    """The compact idle wordmark stays centered above a ready launchpad."""
    import operator_tasks as OT
    OT.save_task({"name": "Harness saved task", "prompt": "Open example.com",
                  "sites": "example.com"})
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v1', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        _expand_launchpad(pg)
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
        assert metrics["font"].startswith('"Plus Jakarta Sans"')
        assert 36 <= metrics["size"] <= 40
        assert metrics["tracking"] >= -0.035 * metrics["size"]
        assert metrics["centerDelta"] <= 2

        pg.wait_for_selector(".op-lp-card", state="visible", timeout=8000)
        assert pg.locator(".op-lp-card").count() == 6
        assert pg.locator("#op-lp-add").is_visible()
        assert pg.locator(".op-lp-new").count() == 0
        pill_rows = pg.locator(".op-lp-cat").evaluate_all(
            "els => new Set(els.filter(el => el.getBoundingClientRect().width)"
            ".map(el => Math.round(el.getBoundingClientRect().top))).size")
        assert pill_rows == 1
        composer = pg.locator(".op-lp-composer").evaluate(
            """el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
              return {width: r.width, height: r.height, radius: parseFloat(s.borderRadius)}; }""")
        assert 560 <= composer["width"] <= 590
        assert 40 <= composer["height"] <= 44
        assert composer["radius"] >= composer["height"] / 2 - 1
        input_metrics = pg.locator("#op-lp-input").evaluate(
            """el => { const r = el.getBoundingClientRect(); const c = el.parentElement.getBoundingClientRect();
              return {font: parseFloat(getComputedStyle(el).fontSize),
                      placeholder: parseFloat(getComputedStyle(el, '::placeholder').fontSize),
                      centerDelta: Math.abs((r.top + r.bottom) / 2 - (c.top + c.bottom) / 2)}; }""")
        assert input_metrics["font"] == input_metrics["placeholder"]
        assert 11.3 <= input_metrics["font"] <= 11.6
        assert input_metrics["centerDelta"] <= 1
        assembly = pg.locator("#op-lp").evaluate(
            """lp => {
              const stage = document.getElementById('op-stage').getBoundingClientRect();
              const els = ['.op-lp-hero', '.op-lp-bar', '.op-lp-grid'].map(s => lp.querySelector(s));
              const boxes = els.map(el => el.getBoundingClientRect());
              const top = Math.min(...boxes.map(r => r.top));
              const bottom = Math.max(...boxes.map(r => r.bottom));
              return {centerDelta: Math.abs((top + bottom) / 2 - (stage.top + stage.bottom) / 2)};
            }""")
        assert assembly["centerDelta"] <= 6
        heading_size = pg.locator("#op-lp-title").evaluate(
            "el => parseFloat(getComputedStyle(el).fontSize)")
        assert 20 <= heading_size <= 23
        action_metrics = pg.locator(".op-lp-actions > button:not([hidden])").evaluate_all(
            """els => ({ids: els.map(el => el.id), gaps: els.slice(1).map((el, i) =>
              el.getBoundingClientRect().left - els[i].getBoundingClientRect().right)})""")
        assert action_metrics["ids"] == ["op-lp-search", "op-lp-refresh", "op-lp-add"]
        assert max(action_metrics["gaps"]) - min(action_metrics["gaps"]) <= 1
        assert pg.locator("#op-lp-add .op-lp-add-ico").count() == 1
        send_metrics = pg.locator("#op-lp-send").evaluate(
            """el => ({size: el.getBoundingClientRect().width,
                        glyph: el.querySelector('svg').getBoundingClientRect().width})""")
        assert 29 <= send_metrics["size"] <= 31
        assert 15 <= send_metrics["glyph"] <= 17
        assert pg.locator("#op-lp-add svg").evaluate(
            "el => el.getBoundingClientRect().width") == 14
        assert pg.locator("#op-agent-cursor").evaluate(
            "el => getComputedStyle(el).display") == "none"
        assert pg.locator("#op-steer-cursor").evaluate(
            "el => getComputedStyle(el).display") == "none"
        assert pg.locator(".op-kbhint").evaluate(
            "el => getComputedStyle(el).display") == "none"
        corner = pg.evaluate("""() => {
          const t = document.getElementById('op-lp-theme').getBoundingClientRect();
          const x = document.getElementById('op-lp-x').getBoundingClientRect();
          const themeCenterOffset = (t.top + t.bottom) / 2 - (x.top + x.bottom) / 2;
          return {centerDelta: Math.abs(themeCenterOffset), themeCenterOffset,
                  themeSize: t.width, closeSize: x.width, themeRight: t.right,
                  closeLeft: x.left, closeRight: x.right, viewport: innerWidth};
        }""")
        assert 2.75 <= corner["themeCenterOffset"] <= 3.25
        assert corner["themeSize"] == corner["closeSize"] == 32
        assert corner["themeRight"] < corner["closeLeft"]
        assert corner["viewport"] - corner["closeRight"] <= 20
        assert pg.locator("#op-lp-theme").get_attribute("aria-label") == "use light mode"
        assert pg.locator("#op-lp-x svg path").count() == 1
        assert "M3 3l8 8M11 3l-8 8" in \
            pg.locator("#op-lp-x svg path").get_attribute("d")
        pg.wait_for_timeout(500)  # let the staggered card entrance finish before measuring bounds
        grid_bottom = pg.locator("#op-lp-grid").evaluate(
            "el => el.getBoundingClientRect().bottom")
        card_bottom = pg.locator(".op-lp-card").evaluate_all(
            "els => Math.max(...els.map(el => el.getBoundingClientRect().bottom))")
        assert card_bottom <= grid_bottom + 1

        for category in ("research", "media"):
            pg.click(f'.op-lp-cat[data-category="{category}"]')
            pg.wait_for_timeout(240)
            assert pg.locator(".op-lp-card").count() == 6
        pg.click('.op-lp-cat[data-category="all"]')
        pg.wait_for_timeout(240)

        pg.click("#op-lp-tasks-toggle")
        pg.wait_for_timeout(240)
        assert pg.locator("#op-lp-title").text_content() == "Saved tasks"
        assert pg.locator("#op-lp-tasks-toggle").get_attribute("aria-pressed") == "true"
        pg.click('.op-lp-cat[data-category="all"]')
        pg.wait_for_timeout(240)
        assert pg.locator("#op-lp-title").text_content() == "Things to do with Operator"
        assert pg.locator(".op-lp-card").count() == 6

        pg.click("#op-lp-add")
        assert not pg.locator("#op-nt-veil").is_hidden()
        modal_type = pg.locator("#op-nt-prompt").evaluate(
            """el => ({typed: parseFloat(getComputedStyle(el).fontSize),
                        placeholder: parseFloat(getComputedStyle(el, '::placeholder').fontSize)})""")
        assert 10.5 <= modal_type["typed"] <= 11.2
        assert modal_type["placeholder"] == modal_type["typed"]
        assert pg.locator('label[for="op-nt-sites"]').text_content() == \
            "Websites and tools Operator can use"
        pg.fill("#op-nt-sites", "doordash.com")
        pg.press("#op-nt-sites", "Enter")
        assert pg.locator('.op-nt-pill[data-v="doordash.com"] > span').first.text_content() == \
            "DoorDash"
        pg.locator("#op-nt-cancel").evaluate("el => el.click()")
        assert pg.locator("#op-nt-veil").is_hidden()

        pg.click("#op-lp-theme")
        assert pg.locator("#op-lp-theme").get_attribute("aria-label") == "use dark mode"
        pg.wait_for_timeout(220)
        light_surfaces = pg.evaluate("""() => {
          const surfaces = {
            composer: document.querySelector('.op-lp-composer'),
            pill: document.querySelector('.op-lp-cat:not(.active):not([hidden])'),
            card: document.querySelector('.op-lp-card')
          };
          return {theme: document.documentElement.getAttribute('data-theme'),
            matches: surfaces.card.matches('[data-theme="light"] .op-lp-card'),
            cardBorder: getComputedStyle(surfaces.card).borderColor,
            colors: Object.fromEntries(Object.entries(surfaces).map(
              ([key, el]) => [key, getComputedStyle(el).backgroundColor]))};
        }""")
        assert light_surfaces == {
            "theme": "light",
            "matches": True,
            "cardBorder": "rgb(228, 231, 235)",
            "colors": {"composer": "rgb(255, 255, 255)",
                       "pill": "rgb(255, 255, 255)",
                       "card": "rgb(255, 255, 255)"},
        }
    finally:
        ctx.close()


def test_launchpad_backdrop_collapses_results_and_theme_toggle_is_local(browser, harness):
    """Empty-space clicks compact the splash; category and theme controls remain useful."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v1', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        _expand_launchpad(pg)
        hero_top = pg.locator(".op-lp-hero").bounding_box()["y"]

        pg.mouse.click(20, 450)
        pg.wait_for_timeout(500)
        assert "op-lp-collapsed" in pg.locator("#op-lp").get_attribute("class")
        assert pg.locator(".op-lp-results-inner").bounding_box()["height"] < 1
        assert pg.locator(".op-lp-hero").bounding_box()["y"] > hero_top + 50
        assert pg.locator("#op-lp-input").is_visible()
        assert pg.locator(".op-lp-cats").is_visible()
        assert pg.locator(".op-lp-cat.active").count() == 0

        pg.click('.op-lp-cat[data-category="media"]')
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
        pg.click("#op-lp-theme")
        assert pg.locator("html").get_attribute("data-theme") == "light"
        assert pg.evaluate("localStorage.getItem('op_theme')") == "light"
        pg.click("#op-lp-theme")
        assert pg.locator("html").get_attribute("data-theme") == "dark"
    finally:
        ctx.close()


def test_operator_origin_and_fullscreen_are_zoom_invariant(browser, harness):
    """Fullscreen keeps its panel frame through viewport changes (8px since
    2026-07-19 "slightly slightly wider", superseding the 6px slim frame that
    itself superseded the 1.0.23 10px spec)."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v1', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="visible", timeout=8000)
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
            for surface in ("body", "op", "launchpad"):
                assert abs(geometry[surface]["x"]) <= 0.5
                assert abs(geometry[surface]["y"]) <= 0.5
                assert abs(geometry[surface]["right"] - geometry["inner"]["w"]) <= 0.5
                assert abs(geometry[surface]["bottom"] - geometry["inner"]["h"]) <= 0.5

        pg.click("#op-lp-x")
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
        "localStorage.setItem('operator-session-v1', "
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
        pg.locator(".op-lp-card").first.click()
        assert pg.locator("#op-lp-input").input_value()

        pg.click('.op-lp-cat[data-category="media"]')
        assert pg.locator('.op-lp-cat[data-category="media"]').get_attribute(
            "aria-pressed") == "true"
        pg.wait_for_timeout(250)
        assert pg.locator(".op-lp-card").count() > 0

        pg.click("#op-lp-x")
        assert pg.locator("#op-lp").is_hidden()
    finally:
        ctx.close()


def test_launchpad_composer_padding_focuses_input_without_selecting_placeholder(browser, harness):
    """Every non-button pixel in the pill focuses input; empty copy is not selectable."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v1', "
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
        "localStorage.setItem('operator-session-v1', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="visible", timeout=8000)
        baseline = pg.evaluate("""() => ({
          input:document.getElementById('op-lp-input').getBoundingClientRect().height,
          composer:document.querySelector('.op-lp-composer').getBoundingClientRect().height})""")

        pg.fill("#op-lp-input", "one\ntwo\nthree\nfour\nfive")
        pg.wait_for_timeout(80)
        expanded = pg.evaluate("""() => {
          const input=document.getElementById('op-lp-input');
          const composer=document.querySelector('.op-lp-composer').getBoundingClientRect();
          const send=document.getElementById('op-lp-send').getBoundingClientRect();
          return {input:input.getBoundingClientRect().height, composer:composer.height,
            client:input.clientHeight, scroll:input.scrollHeight,
            sendBottom:composer.bottom-send.bottom};
        }""")
        assert expanded["input"] >= baseline["input"] * 4.5
        assert expanded["composer"] >= baseline["composer"] + 40
        assert expanded["scroll"] <= expanded["client"] + 1
        assert 4 <= expanded["sendBottom"] <= 7

        pg.fill("#op-lp-input", "wrapped text " * 45)
        pg.wait_for_timeout(80)
        wrapped_height = pg.locator("#op-lp-input").evaluate(
            "el => el.getBoundingClientRect().height")
        assert wrapped_height > baseline["input"] * 2

        pg.fill("#op-lp-input", "short")
        pg.wait_for_timeout(80)
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
        "localStorage.setItem('operator-session-v1', "
        + json.dumps(json.dumps({"log": "", "mode": "auto",
                                 "bot": "", "model": "", "effort": ""})) + ");")
    pg = ctx.new_page()
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="visible", timeout=8000)
        pg.click("#op-lp-x")
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
    """Saved is a PERMANENT category : an empty account keeps the pill and
    its view reads "No saved tasks"; the first save fills it in place."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(
        "localStorage.setItem('operator-session-v1', "
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
        pg.wait_for_timeout(250)
        assert pg.locator("#op-lp-tasks-toggle").is_visible()

        # empty Saved view: pill activates, grid is empty, minimal empty state
        pg.click("#op-lp-tasks-toggle")
        pg.wait_for_timeout(400)
        assert pg.locator("#op-lp-tasks-toggle").get_attribute("aria-pressed") == "true"
        assert pg.locator("#op-lp-title").text_content() == "Saved tasks"
        assert pg.locator(".op-lp-card").count() == 0
        assert pg.locator("#op-lp-empty").is_visible()
        assert pg.locator("#op-lp-empty").text_content() == "No saved tasks"

        pg.click("#op-lp-add")
        pg.fill("#op-nt-name", "Morning brief")
        pg.fill("#op-nt-prompt", "Summarize the morning news")
        pg.click("#op-nt-save")

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
        "localStorage.setItem('operator-session-v1', "
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
        model="gpt-5.6-sol", effort="low", surface="browser", demo=False,
        started_ts=_t.time() - 120, ended_ts=_t.time() - 60,
        _runtime="codex", _cum_in_tokens=1000, _peak_in_tokens=500,
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
        pg.click(".op-hist-row .task")
        pg.wait_for_function(
            "() => { const t = document.querySelector('.op-hist-trace');"
            " return t && t.textContent && !t.textContent.includes('loading'); }",
            timeout=8000)
        assert "found the filings summary" in \
            pg.locator(".op-hist-trace").text_content()
        pg.click(".op-hist-row .task")     # toggle closed again
        pg.wait_for_timeout(300)
        assert pg.locator(".op-hist-trace").count() == 0
        pg.click(".op-hist-rerun")
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
        "localStorage.setItem('operator-session-v1', "
        + json.dumps(json.dumps(_SEEDED_SESSION)) + ");")
    return ctx


def _collectors(pg):
    """pageerror + console-error + /operator/tasks request recorders."""
    errors, con_errors, tasks_reqs = [], [], []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console",
          lambda m: con_errors.append(m.text) if m.type == "error" else None)
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
        pg.click("#op-lp-open")
        pg.wait_for_selector("#op-lp", state="visible", timeout=2000)
        pg.wait_for_timeout(150)
        assert pg.locator(".op-lp-card").count() > 0, "no cards rendered"

        # a card tap drafts into the splash composer (never auto-fires)
        pg.locator(".op-lp-card").first.click()
        assert pg.locator("#op-lp-input").input_value(), "card tap drew blank"

        # a category pill takes the highlight and swaps the grid
        pg.click('.op-lp-cat[data-category="media"]')
        assert pg.locator('.op-lp-cat[data-category="media"]').get_attribute(
            "aria-pressed") == "true"
        pg.wait_for_timeout(300)   # grid cross-fade
        assert pg.locator(".op-lp-card").count() > 0

        # X dismisses; the restored chat is still there underneath
        pg.click("#op-lp-x")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=2000)
        assert pg.locator("#op-log .op-msg").count() >= 2

        # …and HOME still works after the dismissal — wiring survives cycles
        pg.click("#op-lp-open")
        pg.wait_for_selector("#op-lp", state="visible", timeout=2000)
        pg.click('.op-lp-cat[data-category="travel"]')
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
        "localStorage.setItem('operator-session-v1', "
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
        pg.locator(".op-lp-card").first.click()
        assert pg.locator("#op-lp-input").input_value()

        pg.click('.op-lp-cat[data-category="research"]')
        assert pg.locator('.op-lp-cat[data-category="research"]').get_attribute(
            "aria-pressed") == "true"

        pg.click("#op-lp-x")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=2000)
    finally:
        ctx.close()


def test_restored_session_touch_activation(browser, harness):
    """Touch-input pass over the restored-session flow: HOME, card→composer,
    pill highlight, X — all via synthesized taps. Chromium touch emulation is
    the automated BASELINE here, not final iOS acceptance (real-iPad check
    stays a release gate)."""
    ctx = _restored_ctx(browser, has_touch=True,
                        viewport={"width": 1024, "height": 1366})
    pg = ctx.new_page()
    errors, con_errors, _ = _collectors(pg)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=4000)

        pg.tap("#op-lp-open")
        pg.wait_for_selector("#op-lp", state="visible", timeout=2000)
        pg.wait_for_timeout(150)

        pg.locator(".op-lp-card").first.tap()
        assert pg.locator("#op-lp-input").input_value(), "tap drew blank draft"
        # a second card swaps the draft, never stacks or auto-fires
        pg.locator(".op-lp-card").nth(1).tap()
        assert pg.locator("#op-lp-input").input_value()
        assert pg.locator("#op-log .op-msg").count() >= 2   # no dispatch fired

        pg.tap('.op-lp-cat[data-category="shopping"]')
        assert pg.locator('.op-lp-cat[data-category="shopping"]').get_attribute(
            "aria-pressed") == "true"

        pg.tap("#op-lp-x")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=2000)
        assert errors == [], f"JS errors: {errors}"
        assert con_errors == [], f"console errors: {con_errors}"
    finally:
        ctx.close()


# ── 2026-07-18 evening polish: trash presentation + iOS composer geometry ───


def test_trash_clear_returns_to_opaque_splash(browser, harness):
    """Trashing a conversation lands on the SOLID splash, not the translucent
    over-the-feed blur ."""
    ctx = _restored_ctx(browser, viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    errors, con_errors, _ = _collectors(pg)
    try:
        pg.goto(harness.base + "/operator", wait_until="domcontentloaded")
        pg.wait_for_selector("#op-lp", state="hidden", timeout=4000)
        pg.click("#op-clear")
        pg.wait_for_selector("#op-lp", state="visible", timeout=4000)
        assert not pg.eval_on_selector(
            "#op-lp", "el => el.classList.contains('op-lp-over')"), \
            "trash must present the opaque splash, not the blur overlay"
        # the splash it lands on is live: a card drafts into the composer
        pg.wait_for_timeout(150)
        _expand_launchpad(pg)
        pg.locator(".op-lp-card").first.click()
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
        "localStorage.setItem('operator-session-v1', "
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
