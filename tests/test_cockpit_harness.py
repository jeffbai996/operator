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
              "{% block content %}{% endblock %}")

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


def test_midrun_message_steers_not_kills(browser, harness):
    """1.0.12: a message sent while a run is LIVE must soft-steer (POST
    /operator/agent/say) — never stop the run or start a new dispatch (the
    old interrupt-steer behavior). The user bubble renders exactly once (the
    snapshot's role=user echo must not re-render), and the delivery notice
    lands when the queue drains."""
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
        pg.wait_for_timeout(2500)   # a couple of agent polls: say + delivery
        assert harness.say_posts == ["switch to the CAD listing"]
        assert harness.stop_posts == [], "steer must NOT stop the run"
        assert harness.dispatch_posts == [], "steer must NOT re-dispatch"
        assert pg.locator("#op-log .op-msg.user").count() == 1
        # logEvent lines land in the (collapsible) event tray, not the chat log
        evs = pg.locator("#op-events").text_content()
        assert "Steering" in evs and "Steer delivered" in evs
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
