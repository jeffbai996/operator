"""Shared isolated browser fixtures for the operator_view cockpit groups.

Keep dead-CDP guards and per-test store isolation intact. Fixture changes must
select every importing group; test scenarios live in the three cockpit files.
"""

import json
import importlib
import os
import threading
from pathlib import Path


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
    # Each consuming module may follow one that restored the original env.
    os.environ["OPERATOR_DEMO_CDP"] = _DEAD_CDP
    os.environ.pop("OPERATOR_DEMO", None)
    os.environ["OPERATOR_CHROME_LAUNCHER"] = "/nonexistent/operator-harness-launcher"
    os.environ["OPERATOR_SESSION_PATH"] = os.path.join(_HARNESS_STATE_DIR, "session.json")
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
        self.agent_in_tokens = 0
        self.agent_out_tokens = None
        self.say_posts: list = []
        self.stop_posts: list = []
        self.dispatch_posts: list = []
        self.run_posts: list = []
        self.allowed_task_slug = None
        self.frame_tiers: list[str] = []
        self._steer_pending = 0
        # A healthy server answers the stage_size beacon promptly; the
        # entry-size gate tests delay it. `events` records the order in which
        # the server finished it versus frame requests.
        self.steer_delay = 0.0
        self.events: list[tuple[str, float]] = []
        self.beacons: list[str] = []   # stage_size values, in arrival order
        self.steer_not_ready = 0       # answer this many stage_size with ok:false
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
                        "cumulative_in_tokens": self.agent_in_tokens,
                        "cumulative_out_tokens": self.agent_out_tokens,
                        "handoff": self.agent_handoff, "surface": "browser",
                        "steer_pending": pend})
            if self.mode == "real":
                return None
            if (self.steer_delay is not None
                    and request.path.endswith("/operator/steer")):
                body = request.get_json(silent=True) or {}
                if body.get("kind") == "stage_size":
                    import time as _t
                    self.beacons.append(str(body.get("value", "")))
                    if self.steer_not_ready > 0:
                        self.steer_not_ready -= 1
                        self.events.append(("stage_size_not_ready", _t.monotonic()))
                        return jsonify(ok=False, error="streamer not running")
                    _t.sleep(self.steer_delay)
                    self.events.append(("stage_size", _t.monotonic()))
                    return jsonify(ok=True, applied=True, view=[1280, 800])
            if request.path.endswith("/operator/frame"):
                import time as _t
                self.events.append(("frame", _t.monotonic()))
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
def harness(_restore_harness_env):
    h = _Harness()
    yield h
    h.stop()


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True,
                executable_path=os.environ.get("CC_TEST_CHROMIUM") or None)
        except Exception as e:  # noqa: BLE001
            if os.environ.get("OPERATOR_REQUIRE_BROWSER") == "1":
                pytest.fail(f"required headless Chromium unavailable: {e}")
            pytest.skip(f"headless chromium unavailable: {e}")
        new_context = b.new_context

        def offline_context(*args, **kwargs):
            context = new_context(*args, **kwargs)
            # The app is loopback HTTP; external assets need explicit page mocks.
            context.route('https://**', lambda route: route.abort())
            return context

        b.new_context = offline_context
        yield b
        b.close()


@pytest.fixture(autouse=True)
def _fresh_session_store(monkeypatch, harness, tmp_path):
    """Each test gets an empty shared-session store — otherwise a session
    pushed by an earlier test's page boot gets ADOPTED by the next test's
    fresh context (log swap + mode re-apply mid-test = flaky sampling)."""
    # A task fixture must not change the next page's launchpad data source.
    monkeypatch.setattr(harness.mod.operator_tasks_store, 'TASKS_PATH',
                        str(tmp_path / 'saved-tasks.json'))
    harness.mode = "real"
    harness.agent_mode = None
    harness.agent_handoff = None
    harness.agent_messages.clear()
    harness.agent_in_tokens = 0
    harness.agent_out_tokens = None
    harness.say_posts.clear()
    harness.stop_posts.clear()
    harness.dispatch_posts.clear()
    harness.run_posts.clear()
    harness.allowed_task_slug = None
    harness.frame_tiers.clear()
    harness.steer_delay = 0.0
    harness.events.clear()
    harness.beacons.clear()
    harness.steer_not_ready = 0
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
    # The active surface is process state, not session state. A test that
    # opens a desktop-sandbox conversation leaves it selected, and the next
    # page then boots with the launchpad hidden (a game surface hides it).
    OV._active_surface["name"] = "browser"
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
    """Observe the same timer window without waiting for wall-clock time."""
    pg.evaluate(
        """([n, ms]) => {
             const op = document.getElementById('op');
             const t = document.getElementById('op-action-txt');
             const out = window.__signalSamples = [];
             const iv = setInterval(() => {
               out.push({txt: (t && t.textContent || '').trim(),
                         stale: op.classList.contains('op-signal-stale'),
                         lost: op.classList.contains('op-signal-lost')});
               if (out.length >= n) clearInterval(iv);
             }, ms);
           }""",
        [samples, every_ms])
    pg.clock.run_for(samples * every_ms)
    result = pg.evaluate('window.__signalSamples')
    assert len(result) == samples
    return result


def _transitions(values: list) -> int:
    return sum(1 for a, b in zip(values, values[1:]) if a != b)


def _requested_tier(harness, tier: str, timeout: float = 3.0) -> bool:
    """True once a frame request at `tier` reaches the server. The first fetch
    waits for the stage-size answer, so it can trail the tier decision."""
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if tier in harness.frame_tiers:
            return True
        _t.sleep(0.05)
    return tier in harness.frame_tiers


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


def _stage_size(pg) -> str:
    return pg.evaluate(
        "(() => { const r = document.getElementById('op-stage')"
        ".getBoundingClientRect(); return Math.round(r.width) + 'x' + "
        "Math.round(r.height); })()")


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


def _token_host_styles(page):
    # The harness's minimal host omits production colors and its licensed font.
    face = Path(__file__).resolve().parents[2] / 'host-app/static/fonts/AnthropicSansVariable-TextRegular.woff2'
    page.route('**/token-test-font.woff2', lambda route: route.fulfill(
        content_type='font/woff2', body=face.read_bytes()))
    page.add_style_tag(content="""
        @font-face {font-family:'DM Sans';font-style:normal;font-weight:300 800;
          src:url('/token-test-font.woff2') format('woff2')}
        :root {--fg:#e7ecf3;--fg-2:#b8c0cc;--muted:#7e8a9a;--border:#292b30;--border-2:#30343b;--bg:#000}
        body {color:var(--fg);background:var(--bg)}
    """)
    page.evaluate('document.fonts.ready')
