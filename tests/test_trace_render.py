"""RENDERED trace-step typography + the fenced-table promotion.

Same philosophy as test_composer_render: string-pinning what the CSS *says*
misses what the cascade *computes*. These load the real blueprint in headless
Chromium and measure the trace the way an eyeball would.

What's guarded here and why:

  1. The weight LADDER of the action rows (the owner 2026-07-29: "drop them 25 in
     font weight" for the descriptions, "up 25" for the live action word). The
     absolute numbers matter less than the ORDER — verb > label-ish > detail >
     coord — and that DM Sans actually renders 25-step values. It is a
     variable font (100..900); Plus Jakarta Sans, loaded from Google Fonts as
     static 400/500/600/700 instances, does NOT, so a 475/575 pinned on the
     wrong family silently snaps and the re-tune becomes a no-op. That is a
     real trap on this file: an earlier pass mis-read .verb as Plus Jakarta.

  2. The 'n steps' header's LEFT EDGE lines up with the action text below it
     (the owner 2026-07-29: "slightly misaligned to the left compared to the
     action"). It was 0.35rem against the steps' 0.1rem.

  3. A markdown pipe table wrapped in a ``` fence renders as a real <table>,
     not pipe soup in a <pre> (the owner 2026-07-29: "gemini code blocks still not
     rendering tables right"). Gemini fences its tables constantly. The
     companion negative cases — shell pipelines, C bitwise — must stay code.

Run from modules/operator:
  PYTHONPATH=. pytest tests/test_trace_render.py -q
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

# The real _base.html carries the site palette. Without it every var(--…) in
# operator.css is unresolved — and an unresolved var is invalid at
# computed-value time, so the WHOLE declaration falls back to `unset`. For
# `border-left: 1.5px solid var(--border)` that means border-style:none and a
# computed width of 0px, i.e. the trace rail silently lost its 1.5px rule and
# every geometry measurement in this file was 1.5px off production (found
# 2026-07-31, chasing a 1px discrepancy between this harness and the live
# page). Only --border moves layout; the rest are here so the stub is a
# faithful surface rather than a lucky one.
_STUB_VARS = """<style>:root{
  --bg:#0b0d12; --bg-2:#0f1116; --bg-3:#14161c; --bg-4:#1d2029;
  --fg:#e7ecf3; --fg-2:#aab4c2; --muted:#7e8a9a;
  --border:#262c38; --border-2:#333a48; --pill:#1b1f27;
  --accent:#b8c0cc; --live:#3fb950; --bad:#f85149;
}</style>"""
_STUB_BASE = ("<!doctype html><title>{% block title %}{% endblock %}</title>"
              + _STUB_VARS + "{% block content %}{% endblock %}")


@pytest.fixture(scope="module")
def base_url():
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
    """Page JS stays ON here: the trace rows are built by taskActionStep(), and
    the md pipeline is exercised through the real _opMdToHtml hook."""
    with pw_sync.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(base_url + "/operator", wait_until="load")
        pg.wait_for_timeout(400)   # font swap + boot settle
        yield pg
        browser.close()


def _trace_metrics(page) -> dict:
    """Build one action row of each shape and read back computed styles.

    The rows are injected with the SAME class names the runtime uses rather
    than driven through taskActionStep(), so the measurement doesn't depend on
    the live poll loop having produced a task group."""
    return page.evaluate("""() => {
        // Mount inside the REAL #op-log: the trace rows are flex items whose
        // box origin depends on the live rail width, so a detached host with a
        // made-up width measures a different (wrong) geometry.
        const log = document.getElementById('op-log');
        if (!log) throw new Error('no #op-log in the rendered cockpit');
        const host = document.createElement('div');
        host.className = 'op-task';
        host.dataset.busy = '1';
        const steps = document.createElement('div');
        steps.className = 'op-task-steps';
        host.appendChild(steps);

        const cnt = document.createElement('div');
        cnt.className = 'op-step-count';
        cnt.innerHTML = '<span class="sc-n">7 steps</span>';
        steps.appendChild(cnt);

        const row = document.createElement('div');
        row.className = 'op-task-step op-act-step';
        const ico = document.createElement('span');
        ico.className = 'op-act-ico2'; ico.textContent = '*';
        const lab = document.createElement('span');
        lab.className = 'op-act-lab'; lab.textContent = 'Clicking';
        const coord = document.createElement('span');
        coord.className = 'op-act-coord'; coord.textContent = '3 pax trip review';
        lab.appendChild(coord);
        const det = document.createElement('div');
        det.className = 'op-act-detail-plain';
        det.textContent = 'Continue to book Latitude for 3 pax';
        row.appendChild(ico); row.appendChild(lab); row.appendChild(det);
        steps.appendChild(row);

        // the live action word next to the chat spinner. The .ico MUST be here:
        // it carries margin-left, a 1.2rem width AND a 0.19rem margin-right, so
        // a head without it puts the verb 3.04px left of where it really sits —
        // which is most of the alignment error this file guards.
        const head = document.createElement('div'); head.className = 'op-task-head';
        const hico = document.createElement('span'); hico.className = 'ico';
        const verb = document.createElement('span'); verb.className = 'verb';
        verb.textContent = 'Browsing';
        head.appendChild(hico); head.appendChild(verb);
        host.insertBefore(head, steps);

        log.appendChild(host);
        void host.offsetHeight;
        const w = el => parseFloat(getComputedStyle(el).fontWeight);
        // Compare TEXT origins, not box origins: the count and a step row are
        // flex items with different box lefts, so only where the glyphs
        // actually start answers "do these line up".
        const out = {
          verb: w(verb),
          label: w(lab),
          detail: w(det),
          coord: w(coord),
          detailFamily: getComputedStyle(det).fontFamily,
          verbFamily: getComputedStyle(verb).fontFamily,
          countLeft: cnt.querySelector('.sc-n').getBoundingClientRect().left,
          labelLeft: lab.getBoundingClientRect().left,
          detailLeft: det.getBoundingClientRect().left,
          verbLeft: verb.getBoundingClientRect().left,
        };
        host.remove();
        return out;
    }""")


def test_description_weights_sit_below_the_action_label(page):
    """Descriptions dropped 25; the live action word went up 25."""
    m = _trace_metrics(page)
    assert m["detail"] == 475, f"op-act-detail-plain should be 475, got {m['detail']}"
    assert m["coord"] == 375, f"op-act-coord should be 375, got {m['coord']}"
    assert m["verb"] == 575, f"op-task-head .verb should be 575, got {m['verb']}"
    # the ladder, which is the property that actually reads correctly on screen
    assert m["detail"] < m["label"], "description must be lighter than its action label"
    assert m["coord"] < m["detail"], "inline coord is the lightest description form"


def test_tuned_weights_land_on_a_variable_font(page):
    """A 25-step is only real on the variable face the trace was tuned on.

    The ladder above pins 475/575. A static-instance family rounds those to the
    nearest shipped weight and the re-tune silently does nothing, so guard the
    FAMILY or a future 'unify the fonts' pass quietly reverts the look."""
    m = _trace_metrics(page)
    assert "DM Sans" in m["detailFamily"], \
        f"description must resolve to DM Sans, got {m['detailFamily']}"
    assert "DM Sans" in m["verbFamily"], \
        f"live action word must resolve to DM Sans, got {m['verbFamily']}"


def test_step_count_left_edge_matches_the_verb_above_it(page):
    """'n steps' aligns with the VERB — "Typing…" — not the rows below it.

    This guard previously pinned the count to the action ROW's text origin,
    which is what sent the 2026-07-29 fix the wrong way: it moved the count
    0.25rem left to meet the rows and walked it 3.35px past the verb. the owner
    2026-07-31: "i thought the instruction was to align left edge of '4 steps'
    with Typing, went the wrong direction". The reference is the verb.

    The rows sit further left than both and are NOT the target — asserted
    explicitly below so nobody re-derives the old reading from the numbers.
    """
    m = _trace_metrics(page)
    # 0.75px tolerance: .op-task-steps' 1.5px border rounds to 1 device px at
    # DPR 1, which is the entire residual. The pre-fix drift was 3.35px.
    assert abs(m["countLeft"] - m["verbLeft"]) <= 0.75, (
        f"'n steps' left={m['countLeft']} vs verb left={m['verbLeft']} — the "
        "count must share a text origin with the live action word above it")
    assert m["labelLeft"] < m["verbLeft"] - 1, (
        "sanity: the action rows are indented LEFT of the verb, so a fix that "
        "aligns the count to the rows cannot also be aligning it to the verb")


@pytest.mark.parametrize("src,want_table,why", [
    ("```\n| A | B |\n|---|---|\n| 1 | 2 |\n```", True,
     "gemini fences its tables — promote to a real <table>"),
    ("```markdown\n| A | B |\n|---|---|\n| 1 | 2 |\n```", True,
     "language tag on the fence must not block promotion"),
    ("| A | B |\n|---|---|\n| 1 | 2 |", True,
     "control: an unfenced table already worked"),
    ("```\ncat x | grep y | wc -l\n```", False,
     "a shell pipeline is CODE, not a table"),
    ("```\nif (a || b) { x |= 1; }\nreturn x;\nint z = 0;\n```", False,
     "C bitwise/logical-or is CODE"),
    ("```\n| A | B |\n|---|---|\n| 1 | 2 |\nsome trailing note\n```", False,
     "mixed table+prose stays verbatim — promotion requires a PURE table"),
    ("```\n| a | b |\n| c | d |\n| e | f |\n```", False,
     "no |---| separator → not a markdown table"),
])
def test_fenced_pipe_table_promotion(page, src, want_table, why):
    """The strict gate: pure pipe tables become <table>, code stays <pre>."""
    html = page.evaluate("(s) => window._opMdToHtml(s)", src)
    got = "<table" in html
    assert got is want_table, f"{why}\n  src={src!r}\n  html={html[:200]!r}"


@pytest.mark.parametrize("src,selector", [
    ('[safe](https://example.test"/onmouseover="window.__operatorPwned=1)', 'a'),
    ('![safe](https://example.test"/onerror="window.__operatorPwned=1)', 'img'),
])
def test_markdown_urls_cannot_break_out_of_attributes(page, src, selector):
    got = page.evaluate("""([src, selector]) => {
      window.__operatorPwned = 0;
      const host = document.createElement('div');
      host.innerHTML = window._opMdToHtml(src);
      document.body.appendChild(host);
      const node = host.querySelector(selector);
      if (node && selector === 'img') node.dispatchEvent(new Event('error'));
      if (node && selector === 'a') node.dispatchEvent(new MouseEvent('mouseover'));
      const out = {found: !!node,
        eventAttr: node && (node.getAttribute('onerror') || node.getAttribute('onmouseover')),
        executed: window.__operatorPwned, html: host.innerHTML};
      host.remove(); return out;
    }""", [src, selector])
    assert got["found"] is True
    assert got["eventAttr"] is None, got["html"]
    assert got["executed"] == 0, got["html"]
