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
import json
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
HERO_PLACEHOLDER_PX = HERO_INPUT_PX  # merged ed4d5a46: smaller desktop prompt, equal to typed text
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


@pytest.fixture
def interactive_page(base_url, page):
    """Real cockpit JS with the Anthropic font request deliberately paused.

    The model roster normally wins this race on a cold cache.  fitMini then
    measures the fallback face and used to pin that narrower width forever,
    clipping Terra/Sol even though the row had hundreds of spare pixels.
    """
    # Reuse the module browser: starting a second sync_playwright manager while
    # the module-scoped static page owns the first one's asyncio loop is an
    # invalid nested Sync API context.
    context = page.context.browser.new_context(
        viewport={"width": 390, "height": 844})
    pg = context.new_page()
    paused_fonts = []

    def pause_font(route):
        paused_fonts.append(route)

    def isolate_operator_api(route):
        url = route.request.url
        if "/operator/drivers" in url or "/operator/models" in url:
            route.continue_()
            return
        if "/operator/frame" in url or "/operator/stream" in url:
            route.fulfill(status=204)
            return
        # Running the cockpit JS must not attach a second test streamer to
        # the live :9222 Chrome. The picker needs only drivers/models; all
        # other polling and mutation endpoints get a benign local shell.
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True, "can_control": True, "status": "idle",
            "surface": "browser", "browser_up": True,
            "tabs": [], "tasks": [], "surfaces": [], "maps": [],
        }))

    pg.route("**/operator/**", isolate_operator_api)
    pg.route("**/*.woff2", pause_font)
    pg.goto(base_url + "/operator", wait_until="domcontentloaded")
    pg.wait_for_function(
        "document.getElementById('op-modelrow').classList.contains('op-ready')")
    pg.wait_for_timeout(100)
    yield pg, paused_fonts
    for route in paused_fonts:
        try:
            route.abort()
        except Exception:
            pass
    context.close()


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
    assert m["phFont"] == pytest.approx(_typed_font(page, ".op-lp-input"), abs=.25)


def test_hero_empty_and_typed_boxes_keep_the_merged_compact_scale(page):
    """The September desktop fix removed the larger empty-state font. Both
    states now share a line box, while the separate clipping guard remains."""
    def height(value: str) -> float:
        page.evaluate("""(v) => {
            const op = document.getElementById('op');
            op.classList.remove('op-booting'); op.classList.add('op-ready');
            const lp = document.getElementById('op-lp');
            lp.classList.remove('op-lp-collapsed'); lp.removeAttribute('hidden');
            const input = document.querySelector('.op-lp-input');
            // This assertion compares the two settled CSS states. Chromium
            // can retain the previous used height when the declaration stays
            // `1.3em` and only its font-relative basis changes mid-transition.
            input.style.transition = 'none';
            input.value = v;
            void input.offsetHeight;
        }""", value)
        # `transition: height .16s` on .op-lp-input — a synchronous read after
        # setting .value returns the PRE-transition height, which made empty
        # and typed look identical and fake-passed this guard.
        page.wait_for_timeout(320)
        return page.evaluate(
            "() => document.querySelector('.op-lp-input')"
            ".getBoundingClientRect().height")

    b = {"empty": height(""), "typed": height("x")}
    assert b['empty'] == pytest.approx(b['typed'], abs=.5)


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


def test_desktop_hero_placeholder_clears_on_focus(page):
    """A blank focused composer should show the caret, not prompt copy."""
    opacity = page.evaluate("""() => {
        const input = document.querySelector('.op-lp-input');
        input.value = '';
        input.focus();
        return parseFloat(getComputedStyle(input, '::placeholder').opacity);
    }""")
    assert opacity == pytest.approx(0, abs=0.01)
    page.evaluate("document.querySelector('.op-lp-input').blur()")


def test_driver_emoji_centered_in_picker(page):
    delta = page.evaluate("""() => {
        const wrap = document.getElementById('op-pick-wrap');
        wrap.hidden = false;
        const face = document.getElementById('op-pick-face');
        const wr = wrap.getBoundingClientRect();
        const fr = face.getBoundingClientRect();
        return {
          x: (fr.left + fr.width / 2) - (wr.left + wr.width / 2),
          y: (fr.top + fr.height / 2) - (wr.top + wr.height / 2),
        };
    }""")
    assert abs(delta["x"]) <= 0.1, f"driver emoji off center horizontally by {delta['x']:.2f}px"
    assert abs(delta["y"]) <= 0.1, f"driver emoji off center vertically by {delta['y']:.2f}px"


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


def test_empty_chat_input_and_send_button_share_one_centerline(page):
    """The empty desktop composer is one row, not two vertically drifting
    controls. Once text grows beyond one line the send button may pin low, but
    the placeholder and button must share a centerline in the idle state."""
    d = page.evaluate("""() => {
        const op = document.getElementById('op');
        op.classList.remove('op-booting'); op.classList.add('op-ready');
        document.getElementById('op-lp').hidden = true;
        const input = document.getElementById('op-input');
        input.value = '';
        void op.offsetHeight;
        const i = input.getBoundingClientRect();
        const s = document.getElementById('op-send').getBoundingClientRect();
        return (i.top + i.height / 2) - (s.top + s.height / 2);
    }""")
    assert abs(d) <= 1.0, (
        f"empty chat input and send button differ by {d:.2f}px vertically")


def test_chat_composer_keeps_dense_default_chrome(page):
    """The fixed-size composer must not regain a padded-out empty shell.

    Measure only non-content chrome: outer padding, the divider gap, and
    borders. Text/model line heights remain free to follow font rendering.
    """
    d = page.evaluate("""() => {
        const op = document.getElementById('op');
        op.classList.remove('op-booting'); op.classList.add('op-ready');
        op.style.setProperty('--op-composer-compact', 0);
        document.getElementById('op-lp').hidden = true;
        void op.offsetHeight;
        const height = sel => document.querySelector(sel).getBoundingClientRect().height;
        const box = height('.op-inputbox');
        const steer = height('.op-steer');
        const model = height('.op-modelrow');
        return {box, steer, model, chrome: box - steer - model};
    }""")
    assert d["box"] <= 61.0, (
        f"composer is too tall ({d['box']:.2f}px): {d}")
    assert d["chrome"] <= 12.5, (
        f"composer chrome is too loose ({d['chrome']:.2f}px): {d}")


def test_chat_composer_finishes_shrinking_at_the_smallest_notches(page):
    """The last two A− notches used to shrink only the chat text.

    That made the composer look comically overbuilt at the accessibility
    minimum: the text got smaller while its chrome stayed at the normal size.
    Pin the rendered geometry, not a selector spelling, so future tuning can
    keep the deliberate compact range without reintroducing that mismatch.
    """
    def size(compact: float) -> dict:
        return page.evaluate("""(compact) => {
            const op = document.getElementById('op');
            op.classList.remove('op-booting'); op.classList.add('op-ready');
            op.style.setProperty('--op-composer-compact', compact);
            document.getElementById('op-lp').hidden = true;
            void op.offsetHeight;
            const box = document.querySelector('.op-inputbox').getBoundingClientRect();
            const send = document.querySelector('.op-send').getBoundingClientRect();
            const model = document.querySelector('.op-modelrow').getBoundingClientRect();
            return {box: box.height, send: send.height, modelTop: model.top};
        }""", compact)

    normal = size(0)
    penultimate = size(0.5)
    smallest = size(1)
    assert normal["box"] > penultimate["box"] > smallest["box"], (
        f"composer did not finish compacting: {normal}, {penultimate}, {smallest}")
    # Compact the shell, not the primary action into a postage stamp. The
    # button still follows --chat-scale, so A-/A+ remains coherent; the extra
    # compact variable must not punish it a second time at the last two steps.
    assert normal["send"] == pytest.approx(penultimate["send"], abs=0.15)
    assert normal["send"] == pytest.approx(smallest["send"], abs=0.15)


def _composer_gaps(page, compact: int) -> dict:
    """The four vertical gaps inside the chat composer, at a given A− notch."""
    return page.evaluate("""(compact) => {
        const op = document.getElementById('op');
        op.classList.remove('op-booting'); op.classList.add('op-ready');
        const lp = document.getElementById('op-lp');
        lp.classList.remove('op-lp-collapsed'); lp.removeAttribute('hidden');
        op.style.setProperty('--op-composer-compact', compact);
        void op.offsetHeight;
        const box = document.querySelector('.op-inputbox');
        const row = document.getElementById('op-modelrow');
        const ta  = document.getElementById('op-input');
        const mini = document.getElementById('op-model');
        const B = e => e.getBoundingClientRect();
        // Discount --op-type-nudge: it deliberately shifts the TYPE off the
        // box centre for optical reasons, and this test is about the BOXES.
        // The nudge is guarded separately by the optical-centring test.
        const nudge = el => parseFloat(getComputedStyle(el).top) || 0;
        const nt = nudge(ta), nm = nudge(mini);
        const b = B(box), sep = B(row).top, t = B(ta), m = B(mini);
        return { top: (t.top - nt) - b.top, above: sep - (t.bottom - nt),
                 below: (m.top - nm) - sep, bottom: b.bottom - (m.bottom - nm),
                 height: b.height };
    }""", compact)


@pytest.mark.parametrize("compact", [0, 1])
def test_composer_rows_sit_equidistant_from_their_borders(page, compact):
    """Both rows must be centred in their half of the box.

    The shell padded 0.28rem while the separator gaps were 0.18rem, so the
    input AND the model row each sat ~1.6px closer to the separator than to
    the outer border — "model/effort are too high up, the Message Operator
    too low" (the owner 2026-08-31). One `--op-composer-gap` now feeds all four,
    which is the part that keeps them from drifting apart again.
    """
    g = _composer_gaps(page, compact)
    gaps = [g["top"], g["above"], g["below"], g["bottom"]]
    assert max(gaps) - min(gaps) < 0.15, (
        f"composer gaps uneven at compact={compact}: "
        f"{[round(v, 2) for v in gaps]}")


@pytest.mark.parametrize(("compact", "total_rem"), [(0, 0.92), (1, 0.62)])
def test_equalising_the_gaps_kept_the_composer_the_same_height(
        page, compact, total_rem):
    """0.23rem is the MEAN of the old 0.28/0.18 on purpose: the rows move, the
    box does not. Asserting the summed gap rather than a pixel height keeps
    this independent of the font that happens to be loaded — and 4 x 0.23 is
    the old 0.28+0.18+0.18+0.28 exactly, which is the whole reason that value
    was chosen. A re-tune that grows the composer has to change this number
    and say so."""
    g = _composer_gaps(page, compact)
    total = g["top"] + g["above"] + g["below"] + g["bottom"]
    assert abs(total - total_rem * REM) < 0.25, (
        f"composer gap budget moved at compact={compact}: "
        f"{total:.2f}px vs {total_rem * REM:.2f}px")


def test_model_label_stays_on_the_composer_left_rail(page):
    """The model name and the input's placeholder share one left edge.

    Matching .op-mini's left inset to its 1.35em caret gutter centres the label
    inside its hover pill — and shoves it 14.86px off this rail, which is worse
    and visible without hovering. Shipped and reverted the same night
    (the owner 2026-08-31, "this is still what i see"). The label cannot be both
    rail-aligned and pill-centred while the caret shares the box; the rail
    wins, because it is on screen all the time.
    """
    rail = page.evaluate("""() => {
        const op = document.getElementById('op');
        op.classList.remove('op-booting'); op.classList.add('op-ready');
        const lp = document.getElementById('op-lp');
        lp.classList.remove('op-lp-collapsed'); lp.removeAttribute('hidden');
        void op.offsetHeight;
        const box = document.querySelector('.op-inputbox');
        const ta  = document.getElementById('op-input');
        const mi  = document.getElementById('op-model');
        const B = e => e.getBoundingClientRect();
        const b = B(box);
        const inputInk = B(ta).left + parseFloat(getComputedStyle(ta).paddingLeft) - b.left;
        const modelInk = B(mi).left + parseFloat(getComputedStyle(mi).paddingLeft) - b.left;
        return {input: inputInk, model: modelInk};
    }""")
    off = abs(rail["model"] - rail["input"])
    assert off < 1.5, (
        f"model label is {off:.2f}px off the input's left rail "
        f"(input {rail['input']:.2f}, model {rail['model']:.2f})")


def test_model_picker_refits_after_its_webfont_loads(interactive_page):
    """A cold font cache must not permanently ellipsize Terra/Sol.

    Plenty of row width is available here.  We pause the actual webfont,
    measure Terra through the same public fitter used by roster/model changes,
    then release the font and assert the final glyphs still fit before the
    caret gutter.
    """
    page, paused_fonts = interactive_page
    assert paused_fonts, "Anthropic font request was not intercepted"
    page.evaluate("""() => {
        const model = document.getElementById('op-model');
        model.textContent = '';
        const option = document.createElement('option');
        option.value = 'gpt-5.6-terra';
        option.textContent = 'GPT-5.6 Terra';
        model.append(option);
        model.value = option.value;
        window._opFitModel();
        // This is the exact cold-cache failure measured in host-app: the
        // fallback face pinned a 92px outer box, whose 71px content well is
        // too narrow once DM Sans finishes loading.
        model.style.width = '92px';
        model.style.minWidth = '92px';
    }""")
    before = page.locator("#op-model").evaluate("el => el.getBoundingClientRect().width")
    for route in paused_fonts:
        route.continue_()
    page.wait_for_function("document.fonts.status === 'loaded'")
    page.wait_for_timeout(50)
    result = page.evaluate("""() => {
        const model = document.getElementById('op-model');
        const cs = getComputedStyle(model);
        const canvas = document.createElement('canvas').getContext('2d');
        canvas.font = cs.font;
        const ink = canvas.measureText(model.selectedOptions[0].textContent).width;
        const content = model.clientWidth
          - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
        return {ink, content, width: model.getBoundingClientRect().width,
                row: document.getElementById('op-modelrow').getBoundingClientRect().width};
    }""")
    assert result["content"] >= result["ink"], (
        f"model remains clipped after font load: {result}")
    assert result["width"] > before + 1, (
        f"picker never refit its fallback-font width: before={before}, after={result}")
    assert result["width"] < result["row"] * 0.6, (
        f"model picker consumed the row instead of staying compact: {result}")


@pytest.mark.parametrize("width", [390, 1280])
def test_effort_picker_fits_selected_label(interactive_page, width):
    page, fonts = interactive_page
    page.set_viewport_size({"width": width, "height": 900})
    # Safari uses the native select fallback, which sizes to the longest
    # option rather than the selected one. Exercise that path in Chromium.
    page.add_style_tag(content="#op #op-effort{appearance:none;-webkit-appearance:none}")
    for route in fonts:
        route.continue_()
    page.wait_for_function("document.fonts.status === 'loaded'")
    page.evaluate("""() => {
        const model = document.getElementById('op-model');
        model.replaceChildren(new Option('GPT-6 Astra', 'gpt-6-astra'));
        model.dispatchEvent(new Event('change'));
    }""")
    for effort in ("low", "medium", "ultra", "low"):
        page.select_option("#op-effort", effort, force=True)
        measured = page.locator("#op-effort").evaluate("""el => {
            const cs = getComputedStyle(el);
            const span = document.createElement('span');
            span.style.cssText = 'position:absolute;white-space:nowrap';
            span.style.font = cs.font;
            span.textContent = el.selectedOptions[0].textContent;
            document.body.append(span);
            const ink = span.getBoundingClientRect().width;
            span.remove();
            const content = el.clientWidth - parseFloat(cs.paddingLeft)
                - parseFloat(cs.paddingRight);
            return {ink, content};
        }""")
        assert measured["content"] >= measured["ink"] - 1
        assert measured["content"] - measured["ink"] <= 4, measured


_NOTCHES = (0.82, 0.90, 0.94, 0.98, 1.05, 1.13, 1.20, 1.28)


def _at_scale(page, scale: float) -> dict:
    return page.evaluate("""(scale) => {
        const op = document.getElementById('op');
        op.classList.remove('op-booting'); op.classList.add('op-ready');
        const lp = document.getElementById('op-lp');
        lp.classList.remove('op-lp-collapsed'); lp.removeAttribute('hidden');
        op.style.setProperty('--chat-scale', String(scale));
        op.style.setProperty('--op-composer-compact',
            Math.max(0, Math.min(1, (0.90 - scale) / 0.08)).toFixed(3));
        void op.offsetHeight;
        const B = e => e.getBoundingClientRect();
        const out = {
            box:   B(document.querySelector('.op-inputbox')).height,
            steer: B(document.querySelector('.op-steer')).height,
            input: B(document.getElementById('op-input')).height,
            send:  B(document.querySelector('.op-send')).height,
        };
        // where the CAP BAND sits inside the line box, per row
        const c = document.createElement('canvas').getContext('2d');
        for (const [k, sel, txt] of [['inputCap', '#op-input', 'Message Operator'],
                                     ['miniCap',  '#op-model', 'GPT-6 Luna']]) {
            const el = document.querySelector(sel), cs = getComputedStyle(el);
            c.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
            const m = c.measureText(txt);
            const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.2;
            const asc = m.fontBoundingBoxAscent, desc = m.fontBoundingBoxDescent;
            const baseline = (lh - (asc + desc)) / 2 + asc;
            const nudge = parseFloat(cs.top) || 0;
            out[k] = (baseline - m.actualBoundingBoxAscent / 2) - lh / 2 + nudge;
        }
        return out;
    }""", scale)


def test_the_send_button_never_sets_the_input_row_height(page):
    """A fixed 21px button out-heighted the type and pinned the row.

    That is what froze the composer at 59.69px across three A+ notches while
    the text kept growing (the owner 2026-08-31, "composer size doesnt respond
    beyond a few notches"). The button scales with --chat-scale now; the row
    must take its height from the text at every notch.
    """
    for s in _NOTCHES:
        m = _at_scale(page, s)
        assert m["send"] <= m["input"] + 0.5, (
            f"send button ({m['send']:.2f}px) out-heights the input "
            f"({m['input']:.2f}px) at scale {s} — it will pin the row")
        assert abs(m["steer"] - m["input"]) < 0.5, (
            f"input row is {m['steer']:.2f}px for {m['input']:.2f}px of text "
            f"at scale {s} — something other than the type is setting it")


def test_smallest_notch_keeps_send_legible_and_typed_ink_centered(page):
    """Pin the two failures visible at the accessibility minimum: the Enter
    control was compacted twice, and typed text sat above its visual centre.
    The cap-band measurement catches font/padding drift that box centring does
    not — this is the invariant behind the screenshot, not a magic padding
    string."""
    m = _at_scale(page, _NOTCHES[0])
    assert m["send"] >= 16.0, \
        f"send control is squashed at the smallest notch: {m['send']:.2f}px"
    assert -0.05 <= m["inputCap"] <= 0.25, (
        f"typed ink rides off centre at the smallest notch: "
        f"{m['inputCap']:.2f}px")


def test_stop_glyph_is_dead_center_in_its_button(page):
    delta = page.evaluate("""() => {
        const button = document.getElementById('op-send');
        const original = button.innerHTML;
        button.classList.add('stopping');
        button.innerHTML = '<svg viewBox="0 0 24 24" width="11" height="11" '
          + 'fill="currentColor"><rect x="5" y="5" width="14" height="14" '
          + 'rx="2.5"></rect></svg>';
        const b = button.getBoundingClientRect();
        const s = button.querySelector('svg').getBoundingClientRect();
        const result = {x: (s.left + s.width / 2) - (b.left + b.width / 2),
                        y: (s.top + s.height / 2) - (b.top + b.height / 2)};
        button.classList.remove('stopping');
        button.innerHTML = original;
        return result;
    }""")
    assert abs(delta["x"]) <= 0.1 and abs(delta["y"]) <= 0.1, \
        f"stop glyph is not centred: {delta}"


def test_every_notch_actually_resizes_the_composer(page):
    """No dead steps: each A−/A+ press has to move the box."""
    heights = [_at_scale(page, s)["box"] for s in _NOTCHES]
    frozen = [(a, b) for a, b, h1, h2 in
              zip(_NOTCHES, _NOTCHES[1:], heights, heights[1:])
              if abs(h2 - h1) < 0.25]
    assert not frozen, (
        f"composer height does not respond between notches {frozen}; "
        f"heights {[round(h, 2) for h in heights]}")


def test_the_default_notch_keeps_its_established_geometry(page):
    """1.05 is the shipped default — the scaling rewrite must not move it."""
    m = _at_scale(page, 1.05)
    assert abs(m["send"] - 21.0) < 0.2, f"send button drifted to {m['send']:.2f}px"
    assert abs(m["box"] - 60.61) < 0.6, f"composer drifted to {m['box']:.2f}px"


def test_task_editor_fields_are_true_black_in_dark_mode(page):
    """The task editor is already a raised panel; its editable wells should
    read as input, not as another stack of grey panels inside it."""
    fills = page.evaluate("""() => {
        const op = document.getElementById('op');
        op.classList.remove('op-booting'); op.classList.add('op-ready');
        document.documentElement.dataset.theme = 'dark';
        const veil = document.getElementById('op-nt-veil');
        veil.hidden = false;
        const colors = ['#op-nt-name', '#op-nt-prompt', '#op-nt-pills', '#op-nt-rep']
          .map(sel => getComputedStyle(document.querySelector(sel)).backgroundColor);
        veil.hidden = true;
        return colors;
    }""")
    assert set(fills) == {"rgb(0, 0, 0)"}, f"task field fills drifted: {fills}"


def test_browser_menu_quality_leads_and_trailing_controls_have_real_semantics(page):
    """The right rail may contain a keyboard shortcut OR an action icon.
    Random Unicode in identical keycap boxes is neither, and was how arrows,
    a house, a squiggle, and an escape rune all wound up pretending to be
    shortcuts."""
    page.set_viewport_size({"width": 390, "height": 844})
    try:
        result = page.evaluate("""() => {
          const menu = document.getElementById('op-ham-menu');
          menu.hidden = false;
          menu.style.top = '8px'; menu.style.right = '8px'; menu.style.left = 'auto';
          menu.style.maxHeight = '828px';
          const children = [...menu.children];
          const rows = [...menu.querySelectorAll('.op-ham-item')];
          const bad = rows.filter(row => {
            const tail = row.lastElementChild;
            return !tail || (!tail.classList.contains('op-ham-shortcut')
              && !tail.classList.contains('op-ham-icon'));
          }).map(row => row.dataset.kind || row.id || row.textContent.trim());
          const r = menu.getBoundingClientRect();
          const overflow = rows.filter(row => {
            const rr = row.getBoundingClientRect();
            return rr.left < r.left - .5 || rr.right > r.right + .5;
          }).map(row => row.textContent.trim());
          return {
            firstIsQuality: children[0] && children[0].classList.contains('op-ham-quality'),
            dividerAfter: children[1] && children[1].classList.contains('op-ham-sep'),
            bad, overflow,
            horizontalOverflow: menu.scrollWidth > menu.clientWidth + 1,
          };
        }""")
        assert result["firstIsQuality"] is True
        assert result["dividerAfter"] is True
        assert result["bad"] == []
        assert result["overflow"] == []
        assert result["horizontalOverflow"] is False
    finally:
        page.set_viewport_size({"width": 1280, "height": 900})


@pytest.mark.parametrize("row", ["inputCap", "miniCap"])
def test_the_type_is_optically_centred_not_just_box_centred(page, row):
    """Both rows measured dead-centre by box maths and still read high.

    A line box centres the font's em square, and these faces reserve more
    descender room than "Message Operator" or "GPT-6 Luna" use, so the cap
    band sat 0.5-1.0px above centre at every notch — invisible at 1x, obvious
    at the zoom the owner reads it at. --op-type-nudge moves the type, not the box.
    """
    worst = max((abs(_at_scale(page, s)[row]), s) for s in _NOTCHES)
    assert worst[0] < 0.6, (
        f"{row} is {worst[0]:.2f}px off the row's centre at scale {worst[1]}")
