"""RENDERED composer geometry — the anti-regression guard (the owner 2026-07-28:
"we gotta stop regressing the fucking composer, figure out a test based way
to ensure centering and correct font size").

The string-pin tests in test_cockpit_harness assert what the CSS *says*; the
composer regressions keep shipping because what matters is what the cascade
*computes*. These tests serve the real blueprint over HTTP, load it in
headless Chromium, and measure the composer the way an eyeball would:

  1. computed font sizes of input + ::placeholder (hero and chat composer)
  2. the FIT invariant: a placeholder's line box must fit inside the
     textarea's box — the 2026-07-28 bug was a placeholder bumped one notch
     (0.72rem) overflowing a box still sized by the input font (1.3em of
     0.68rem), which topped the text out of center
  3. flex centering: input box centered in its pill/inputbox

Run from modules/operator:
  PYTHONPATH=. pytest tests/test_composer_render.py -q
Requires playwright (+ chromium) in the venv; skips cleanly when absent.
"""
import importlib
import os
import threading

import pytest

pw_sync = pytest.importorskip("playwright.sync_api",
                              reason="playwright not installed")

from flask import Flask
from jinja2 import ChoiceLoader, DictLoader
from werkzeug.serving import make_server

import operator_view as OV

# Expected type scale, in px at the defaults (root 16px, --chat-scale 1.05).
# Deliberate re-tunes repin these constants — that's the point of the guard.
REM = 16.0
SCALE = 1.05
HERO_INPUT_PX = 0.84 * SCALE * REM          # 14.112 (0.80→0.84 "bump one, keep equal", 2026-07-28)
HERO_PLACEHOLDER_PX = 0.96 * SCALE * REM    # 16.128 — SPLASH placeholder is
# deliberately bigger than the typed text (the owner 2026-07-30). The 2026-07-28
# rule was 'always equal', which existed to stop the ride-high; that is now
# held by test_hero_placeholder_line_fits_its_box instead. Note the CSS gets
# here by growing the INPUT's font-size while :placeholder-shown, not the
# ::placeholder's — the empty-state height clamp is 1.3em, so the box grows
# with it and the line box stays proportional. Sizing ::placeholder alone was
# a measured no-op: the 1.3em box clipped the ink identically at 1.08, 1.16
# and 1.24rem. Equality was the old implementation of the safety property,
# not the property itself.
CHAT_INPUT_PX = 0.77 * SCALE * REM          # 12.936 — the ≥821px desktop
# block (source-order winner) sets 0.77rem, not the base 0.74 (the owner 2026-07-15/21)

_STUB_BASE = ("<!doctype html><title>{% block title %}{% endblock %}</title>"
              "{% block content %}{% endblock %}")


@pytest.fixture(scope="module")
def base_url():
    """The real live blueprint on a real ephemeral-port HTTP server —
    playwright needs actual HTTP, not Flask's test_client."""
    os.environ.pop("OPERATOR_DEMO", None)
    mod = importlib.reload(OV)
    app = Flask(__name__)
    app.register_blueprint(mod.bp)
    app.jinja_loader = ChoiceLoader([app.jinja_loader,
                                     DictLoader({"_base.html": _STUB_BASE})])
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def page(base_url):
    """Page JS is DISABLED: the cockpit's own poll loop hides/shows the splash
    on live state, which raced the measurements (rects read 0 mid-hide). The
    composer contract under test is pure CSS; evaluate() still runs with page
    scripts off, so we force-show the splash ourselves and measure a
    deterministic static render."""
    with pw_sync.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1280, "height": 900},
                              java_script_enabled=False)
        pg.goto(base_url + "/operator", wait_until="load")
        pg.wait_for_timeout(250)   # font swap settle
        yield pg
        browser.close()


def _metrics(page, input_sel: str, box_sel: str) -> dict:
    return page.evaluate("""([inputSel, boxSel]) => {
        // page JS is off, so play the boot JS's part statically: without
        // op-ready/uncollapsed classes the cockpit + splash render display:none
        // and every rect reads 0 (which would fake-pass the centering asserts)
        const op = document.getElementById('op');
        op.classList.remove('op-booting'); op.classList.add('op-ready');
        const lp = document.getElementById('op-lp');
        lp.classList.remove('op-lp-collapsed'); lp.removeAttribute('hidden');
        void op.offsetHeight;
        const i = document.querySelector(inputSel);
        const b = document.querySelector(boxSel);
        const cs = getComputedStyle(i);
        const ph = getComputedStyle(i, '::placeholder');
        const ir = i.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return {
          inputFont: parseFloat(cs.fontSize),
          phFont: parseFloat(ph.fontSize),
          phLine: parseFloat(ph.lineHeight),
          boxH: ir.height,
          centerDelta: (ir.top + ir.height / 2) - (br.top + br.height / 2),
        };
    }""", [input_sel, box_sel])


# ── hero (launchpad) composer ────────────────────────────────────────────────

def _typed_font(page, sel: str) -> float:
    """Font-size of the input WITH content in it.

    The splash grows its own font while :placeholder-shown, so an empty box
    reports the placeholder's size, not the typed one. Measuring the typed
    size means typing.
    """
    return page.evaluate("""(sel) => {
        const i = document.querySelector(sel);
        const was = i.value;
        i.value = 'x';
        const px = parseFloat(getComputedStyle(i).fontSize);
        i.value = was;
        return px;
    }""", sel)


def test_hero_font_sizes(page):
    m = _metrics(page, ".op-lp-input", ".op-lp-composer")
    assert _typed_font(page, ".op-lp-input") == pytest.approx(HERO_INPUT_PX, abs=0.25)
    assert m["phFont"] == pytest.approx(HERO_PLACEHOLDER_PX, abs=0.25)
    # the point of the whole exercise: the empty prompt reads bigger than what
    # replaces it. How MUCH bigger is the owner's call (pinned above); that it is
    # bigger at all is the property. Don't pin a delta he tunes by eye.
    assert m["phFont"] > _typed_font(page, ".op-lp-input")


def test_hero_empty_box_grows_with_the_placeholder(page):
    """Pin the MECHANISM, not just the number.

    A dead rule is invisible to the size asserts: 0.96rem is also what the old
    ::placeholder-only rule produced, so phFont alone cannot tell the working
    version from a broken one. On 2026-07-30 a mis-closed CSS comment swallowed
    the entire @media block and every other guard in this file still passed.

    What separates them is the BOX. The empty-state clamp is 1.3em of the
    input's OWN font, so growing that font while :placeholder-shown must grow
    the box with it — which is the whole reason the bigger placeholder is not
    clipped. Empty must therefore be strictly taller than typed.
    """
    def height(value: str) -> float:
        page.evaluate("""(v) => {
            const op = document.getElementById('op');
            op.classList.remove('op-booting'); op.classList.add('op-ready');
            const lp = document.getElementById('op-lp');
            lp.classList.remove('op-lp-collapsed'); lp.removeAttribute('hidden');
            document.querySelector('.op-lp-input').value = v;
        }""", value)
        # `transition: height .16s` on .op-lp-input — a synchronous read after
        # setting .value returns the PRE-transition height, which made empty
        # and typed look identical and fake-passed this guard.
        page.wait_for_timeout(320)
        return page.evaluate(
            "() => document.querySelector('.op-lp-input')"
            ".getBoundingClientRect().height")

    b = {"empty": height(""), "typed": height("x")}
    assert b["empty"] > b["typed"] + 1.0, (
        f"empty box {b['empty']:.2f}px vs typed {b['typed']:.2f}px — the "
        f"empty-state font rule is not applying, so the placeholder is being "
        f"clipped by a box sized for the smaller typed font")


def test_hero_placeholder_line_fits_its_box(page):
    """THE 2026-07-28 regression: placeholder line box taller than the
    textarea ⇒ the text top-anchors and reads high. The placeholder's
    computed line-height must fit the box it renders in."""
    m = _metrics(page, ".op-lp-input", ".op-lp-composer")
    assert m["phLine"] <= m["boxH"] + 0.5, (
        f"placeholder line box {m['phLine']}px overflows the "
        f"{m['boxH']}px textarea — placeholder rides high")


def test_hero_input_centered_in_pill(page):
    m = _metrics(page, ".op-lp-input", ".op-lp-composer")
    # +0.5px deliberate optical nudge (translateY, the owner 2026-07-26)
    assert abs(m["centerDelta"] - 0.5) <= 1.0, (
        f"input box off pill center by {m['centerDelta']:.2f}px")


# ── chat composer ────────────────────────────────────────────────────────────

def test_chat_font_sizes(page):
    m = _metrics(page, "#op-input", ".op-grow-wrap")
    assert m["inputFont"] == pytest.approx(CHAT_INPUT_PX, abs=0.25)
    assert m["phFont"] == pytest.approx(CHAT_INPUT_PX, abs=0.25)


def test_chat_placeholder_line_fits_its_box(page):
    m = _metrics(page, "#op-input", ".op-grow-wrap")
    assert m["phLine"] <= m["boxH"] + 0.5


def test_hero_send_button_centered_when_empty(page):
    """bottom:5px only equals centered while the pill is exactly 42px — at a
    raised chat-scale the pill grows and the button read low (2026-07-28).
    Empty composer must truly center it."""
    d = page.evaluate("""() => {
        const op = document.getElementById('op');
        op.classList.remove('op-booting'); op.classList.add('op-ready');
        const lp = document.getElementById('op-lp');
        lp.classList.remove('op-lp-collapsed'); lp.removeAttribute('hidden');
        void op.offsetHeight;
        const s = document.querySelector('.op-lp-send').getBoundingClientRect();
        const c = document.querySelector('.op-lp-composer').getBoundingClientRect();
        return (s.top + s.height / 2) - (c.top + c.height / 2);
    }""")
    assert abs(d) <= 1.0, f"send button off pill center by {d:.2f}px"


# ── theme-button icon visibility ─────────────────────────────────────────────
# The 2026-07-28 regression: bare-class hide rules lost the specificity fight
# against `.op-lp-theme svg {display:block}` and ALL THREE stop icons rendered
# side by side. Assert exactly one icon visible per theme state, per button.

@pytest.mark.parametrize("state, visible", [
    ("dark", "oled"),    # next stop: OLED  → filled fragify moon
    ("flat", "day"),     # next stop: light → sun
    ("light", "night"),  # next stop: dark  → outline crescent
])
def test_theme_buttons_show_exactly_one_icon(page, state, visible):
    for btn_sel in ("#op-lp-theme", "#op-flat"):
        shown = page.evaluate("""([sel, state]) => {
            const op = document.getElementById('op');
            document.documentElement.setAttribute('data-theme',
                state === 'light' ? 'light' : 'dark');
            op.classList.toggle('op-flat', state === 'flat');
            const out = [];
            for (const k of ['day', 'night', 'oled']) {
              const el = document.querySelector(sel + ' .op-lp-theme-' + k);
              if (el && getComputedStyle(el).display !== 'none') out.push(k);
            }
            document.documentElement.setAttribute('data-theme', 'dark');
            op.classList.remove('op-flat');
            return out;
        }""", [btn_sel, state])
        assert shown == [visible], (
            f"{btn_sel} in {state}: expected only {visible}, got {shown}")


def test_chat_input_centered_in_grow_wrap(page):
    """Reference is .op-grow-wrap (the textarea's row), NOT .op-inputbox —
    the inputbox also holds the model-picker row below, so the input is
    never centered in it by design."""
    m = _metrics(page, "#op-input", ".op-grow-wrap")
    # ~2px of that is structural: the textarea is inline-level in a block
    # wrapper, so the wrap carries the line-box descender gap below it —
    # constant since forever and invisible. 3px catches real drift on top.
    assert abs(m["centerDelta"]) <= 3.0, (
        f"chat input off grow-wrap center by {m['centerDelta']:.2f}px")
