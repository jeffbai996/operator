"""The AUTO cursor's feed: a CDP-dispatched click must reach the page hook.

Operator's cursor overlay follows `window.__opClick`, which an init script sets
from pointerdown/click. The streamer installs that script on `contexts[0]`, and
the control MCP's BrowserSurface binds to `contexts[0]` too — so a macro's
clicks land on a hooked page and the overlay follows them with no extra
plumbing. That conclusion rests entirely on CDP input producing real DOM
events; if it ever stops, the cursor goes dead and the reason is invisible.
So pin it here rather than in a comment.

(The desktop surfaces drive an X display, not a page. Nothing to hook there —
that gap is real and separate.)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pw_sync = pytest.importorskip("playwright.sync_api",
                              reason="needs playwright + chromium")

ROOT = Path(__file__).resolve().parent.parent


def _hook_source() -> str:
    """The init script as it actually ships, lifted from operator_view.py."""
    src = (ROOT / "operator_view.py").read_text(encoding="utf-8")
    m = re.search(r'await ctx\.add_init_script\("""(.*?)"""\)', src, re.S)
    assert m, "the __opClick init script moved — this test cannot verify it"
    hook = m.group(1)
    assert "__opClick" in hook
    return hook


def test_a_cdp_dispatched_click_sets_the_cursor_feed():
    """The whole AUTO-cursor story in one assertion."""
    with pw_sync.sync_playwright() as p:
        b = p.chromium.launch()
        try:
            ctx = b.new_context(viewport={"width": 800, "height": 600})
            ctx.add_init_script(_hook_source())
            pg = ctx.new_page()
            pg.goto("data:text/html,<body style='margin:0'>"
                    "<div style='height:600px'>x</div></body>")
            assert pg.evaluate("!!window.__opClickHooked"), "hook did not install"
            assert pg.evaluate("window.__opClick || null") is None

            sess = ctx.new_cdp_session(pg)
            for typ, extra in (("mouseMoved", {}),
                               ("mousePressed", {"button": "left", "clickCount": 1}),
                               ("mouseReleased", {"button": "left", "clickCount": 1})):
                sess.send("Input.dispatchMouseEvent",
                          {"type": typ, "x": 200.0, "y": 150.0, **extra})
            pg.wait_for_timeout(150)

            got = pg.evaluate("window.__opClick || null")
            assert got, "a CDP click no longer reaches the hook — AUTO cursor is dead"
            # normalized against the viewport, which is what the overlay expects
            assert abs(got["x"] - 0.25) < 0.02, got
            assert abs(got["y"] - 0.25) < 0.02, got
        finally:
            b.close()


def test_the_cursor_stays_present_for_the_whole_run():
    """It used to hide after 5s without a click, so a typing passage read as
    'no cursor' exactly when you were watching to see where the bot was."""
    js = (ROOT / "static/js/operator.js").read_text(encoding="utf-8")
    body = js[js.index("function showAgentClick(c){"):]
    body = body[:body.index("\n  }")]
    assert "_keepAlive" in body, "the run-long keep-alive is gone"
    assert "_inFlight" in body, "the keep-alive must check the run state"
    assert "classList.remove('show')" in body, "an idle stage must still clear"


def test_manual_mode_still_hides_the_agent_cursor():
    """the owner 2026-08-31: 'Keep MAN how it is, I like that.' The agent cursor is
    hidden there in CSS, which is why the keep-alive cannot leak into it."""
    css = (ROOT / "static/operator.css").read_text(encoding="utf-8")
    assert '.op[data-mode="man"] .op-agent-cursor { display: none; }' in css


# ── when the cursor should be PRESENT vs hidden (the owner 2026-09-01) ────────────
# "the cursor can hide if the agent is browsing not visually" — during a
# navigate, a DOM read or a wait there is no screen position, so a cursor left
# sitting on the page is a stale artifact rather than a report of where the
# bot is.

def _js() -> str:
    return (ROOT / "static/js/operator.js").read_text(encoding="utf-8")


def _positional_set() -> set[str]:
    """The allowlist the client actually ships."""
    js = _js()
    block = js[js.index("const ACT_POSITIONAL = new Set(["):]
    block = block[:block.index("]);")]
    return set(re.findall(r"'([^']+)'", block))


def _trace_labels() -> set[str]:
    """Every action label operator_trace can put on the wire."""
    import importlib
    import sys
    sys.path.insert(0, str(ROOT))
    T = importlib.import_module("operator_trace")
    labels: set[str] = set()
    for name in ("_ACTION_LABELS", "_COMPUTER_ACTION_LABELS", "_NONBROWSER_LABELS"):
        labels |= set(getattr(T, name).values())
    return labels


@pytest.mark.parametrize("label", [
    "Clicking", "Double-clicking", "Typing", "Scrolling", "Dragging",
    "Hovering", "Moving", "Selecting", "Filling form",
])
def test_positional_work_keeps_the_cursor(label):
    """Typing counts: it lands on the element the agent just clicked."""
    assert label in _positional_set(), f"{label} happens at a screen position"


@pytest.mark.parametrize("label", [
    "Browsing", "Reading", "Reading console", "Inspecting network", "Waiting",
    "Switching tab", "Going back", "Took screenshot",
])
def test_non_visual_work_lets_the_cursor_go(label):
    assert label not in _positional_set(), f"{label} has no screen position"


def test_the_allowlist_is_a_small_subset_of_what_the_trace_emits():
    """Why an ALLOWLIST and not a denylist.

    The first cut enumerated the non-visual labels and defaulted the rest to
    "show". This test caught it: the trace emits ~90 labels — file edits, web
    searches, memory writes, IBKR calls — and all but a couple of dozen are
    non-positional, so that default held the cursor up through most of what the
    agent does. Hiding is the safe default; only a known positional action
    earns a cursor, and a new tool inherits the safe behaviour instead of an
    accidental one.
    """
    positional, emitted = _positional_set(), _trace_labels()
    assert positional < emitted or positional <= emitted, "allowlist drifted"
    assert len(positional) < len(emitted) / 2, (
        f"the allowlist has grown to {len(positional)} of {len(emitted)} labels "
        "— check it is still only positional actions")
    # every entry must be a label the trace can actually produce
    stale = positional - emitted
    assert not stale, f"allowlist names labels nothing emits: {sorted(stale)}"


def test_the_keep_alive_requires_a_visual_action():
    """The run being live is not enough on its own any more."""
    js = _js()
    body = js[js.index("const _keepAlive = () => {"):]
    body = body[:body.index("};")]
    assert "_lastActionVisual" in body, (
        "the keep-alive re-arms on run-state alone — it will hold a stale "
        "cursor through a navigate or a DOM read")
