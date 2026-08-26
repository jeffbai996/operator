"""Characterization tests for operator_view.py — the Flask blueprint (30+ routes).

Purely additive: pins down CURRENT correct behavior of the routes via Flask's
test client. NO real Chrome, NO real agent runs, NO network — every seam that
would launch something (operator_agent.runner.start, the _Streamer's
run_action / list_tabs / ensure_running, filesystem event/transcript reads) is
mocked or stubbed. Cannot break the running cockpit.

Two app flavors are needed because DEMO gating is a module-level constant read
at import time (`DEMO = os.environ.get("OPERATOR_DEMO") == "1"`). We
importlib.reload the module under each env value to exercise the public-demo /
live-cockpit security boundary — the highest-value assertions here (demo forces
the locked bot/model, and saved tasks only serve a demo-scoped store).

Run (same shape as the sibling operator tests) from modules/operator:
  PYTHONPATH=. pytest tests/test_operator_view.py -q
"""
import asyncio
import importlib
import os
import re
import threading
import time
from pathlib import Path

import pytest
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader

import operator_agent as OA
import operator_view as OV


def test_zoom_split_capture_clip_preserves_requested_viewport_aspect() -> None:
    """A 125% Windows profile zoom must not turn 1280x760 into 1280x608."""
    assert OV._fit_clip_to_view_aspect(1280, 608, 1280, 760) == (1280, 760)
    assert OV._fit_clip_to_view_aspect(1024, 608, 1280, 760) == (1024, 608)


def test_zoom_split_capture_clip_can_expand_width_without_cropping() -> None:
    assert OV._fit_clip_to_view_aspect(1024, 760, 1280, 760) == (1280, 760)


# ---- _emu_clip_box: the capture clip is DEVICE space, always -------------
# Page.captureScreenshot reads `clip` in device pixels. The emulated-regime
# clip was built out of CSS-pixel readings (cssLayoutViewport + body scroll
# box), so on a display-scaled Chrome it cut the canvas down by the scale
# factor and the right column / footer were never captured. Measured live on
# google.com 2026-08-14: css 1036x894, device 1295x1118, body 1036x894 — the
# old arithmetic clipped 1036x905 out of a 1295x1118 canvas (top-left 80%),
# and _fit_clip_to_view_aspect then matched the stage aspect exactly, so it
# read as a narrow page rather than as the crop it was.

def test_emu_clip_covers_the_whole_canvas_on_a_display_scaled_chrome() -> None:
    """The live google.com reading: 1.25 scale must not shrink the clip."""
    assert OV._emu_clip_box(css_w=1036, css_h=894, dev_w=1295, dev_h=1118,
                            body_w=1036, body_h=894, page_x=0, page_y=0,
                            view_w=1280, view_h=1118) == (1295, 1118)


def test_emu_clip_is_unchanged_at_stock_device_scale() -> None:
    assert OV._emu_clip_box(css_w=1280, css_h=1118, dev_w=1280, dev_h=1118,
                            body_w=1280, body_h=1118, page_x=0, page_y=0,
                            view_w=1280, view_h=1118) == (1280, 1118)


def test_emu_clip_never_falls_below_the_painted_canvas() -> None:
    """A body shorter than the viewport must not shrink the frame (the clip
    floor is what kept a short page from capturing a blank tail)."""
    w, h = OV._emu_clip_box(css_w=1036, css_h=894, dev_w=1295, dev_h=1118,
                            body_w=400, body_h=200, page_x=0, page_y=0,
                            view_w=1280, view_h=1118)
    assert (w, h) == (1295, 1118)


def test_emu_clip_follows_the_scroll_offset_in_css_units() -> None:
    """pageX/pageY come off the CSS viewport, so the remaining-extent maths
    must subtract them BEFORE converting to device pixels."""
    w, h = OV._emu_clip_box(css_w=1036, css_h=894, dev_w=1295, dev_h=1118,
                            body_w=1036, body_h=3000, page_x=0, page_y=600,
                            view_w=1280, view_h=1118)
    assert (w, h) == (1295, 1118)


def test_emu_clip_survives_a_missing_body_reading() -> None:
    """The body-extent read is best-effort; a zero must fall back to the
    device viewport, not to a zero-sized clip."""
    assert OV._emu_clip_box(css_w=1036, css_h=894, dev_w=1295, dev_h=1118,
                            body_w=0, body_h=0, page_x=0, page_y=0,
                            view_w=1280, view_h=1118) == (1295, 1118)


# ---- cold start: the geometry must be right on the FIRST frame ----------
# The cockpit beaconed its stage size 1200ms after script run, so every
# session opened at whatever the last viewer left — or at the 1280x0 default,
# whose auto height object-fit renders as a letterbox — until a resize fired
# a fresh beacon. "As soon as I resize it myself it snaps" (the owner 2026-08-15).
# The seed lets the page GET carry the returning viewer's stage, so the very
# first captured frame is already at the right aspect.

def test_clamp_stage_scales_a_narrow_stage_up_keeping_its_aspect() -> None:
    """The measured iPad stage: 704x615 must reach the desktop floor without
    changing shape, or the frame that fills it is the wrong one."""
    w, h = OV._clamp_stage(704, 615)
    assert w == 1280
    assert abs((w / h) - (704 / 615)) < 0.005


def test_clamp_stage_bounds_and_evens_its_output() -> None:
    assert OV._clamp_stage(4000, 4000)[0] <= 1600
    assert OV._clamp_stage(1280, 50)[1] >= 480
    w, h = OV._clamp_stage(1281, 901)
    assert w % 2 == 0 and h % 2 == 0


def test_clamp_stage_rejects_a_degenerate_reading() -> None:
    assert OV._clamp_stage(0, 0) == (0, 0)
    assert OV._clamp_stage(704, 0) == (0, 0)


def test_seed_view_sets_the_target_when_nobody_is_watching() -> None:
    s = OV._Streamer()
    assert s.view_h == 0          # the letterboxing default
    assert s.seed_view_from_stage(704, 615) is True
    assert (s.view_w, s.view_h) == OV._clamp_stage(704, 615)


def test_seed_view_never_reaspects_under_a_live_viewer() -> None:
    """A second device opening the cockpit must not yank the aspect out from
    under whoever is actually watching — the same rule the beacon obeys."""
    s = OV._Streamer()
    s._vp_owner = "watcher"
    s.vp_note_pull("watcher")
    before = (s.view_w, s.view_h)
    assert s.seed_view_from_stage(704, 615) is False
    assert (s.view_w, s.view_h) == before


def test_seed_view_takes_over_from_an_owner_that_stopped_pulling() -> None:
    s = OV._Streamer()
    s._vp_owner = "ghost"
    s._vp_seen["ghost"] = time.monotonic() - (OV.VP_OWNER_IDLE_S + 5)
    assert s.seed_view_from_stage(704, 615) is True


def test_seed_view_is_a_noop_when_the_target_already_matches() -> None:
    s = OV._Streamer()
    s.seed_view_from_stage(704, 615)
    assert s.seed_view_from_stage(704, 615) is False


def test_forced_entry_beacon_reapplies_an_already_matching_target(
        monkeypatch) -> None:
    """A server bounce can leave Chrome at the dead runner's emulation while
    the streamer's desired dimensions still match the returning cockpit.

    The first-load beacon must therefore be able to reapply an unchanged
    target; waiting for a divider drag to change the number is not recovery.
    """
    s = OV._Streamer()
    s._page = object()
    s.view_w, s.view_h = OV._clamp_stage(704, 615)
    applied = []

    async def apply(page):
        applied.append(page)

    async def grab(_page):
        return b""

    monkeypatch.setattr(s, "_apply_view_metrics", apply)
    monkeypatch.setattr(s, "_grab", grab)
    monkeypatch.setattr(OA.runner, "is_running", lambda: False)
    result = asyncio.run(s._do_action({
        "kind": "stage_size", "value": "704x615", "cid": "fresh-tab",
        "force": True,
    }))

    assert result["ok"] is True
    assert result["applied"] is True
    assert applied == [s._page]


def test_cockpit_page_seeds_the_viewport_from_the_stage_cookie(live) -> None:
    """Before the browser paints: the returning viewer's stage rides up on the
    document request, so the streamer's first attach already targets it."""
    client, mod = live
    mod._streamer.view_w, mod._streamer.view_h = mod.DESKTOP_LAYOUT_MIN_W, 0
    mod._streamer._vp_owner = ""
    mod._streamer._vp_seen.clear()
    client.set_cookie("op_stage", "704x615")
    assert client.get("/operator").status_code == 200
    assert (mod._streamer.view_w,
            mod._streamer.view_h) == mod._clamp_stage(704, 615)


def test_cockpit_page_ignores_a_junk_stage_cookie(live) -> None:
    client, mod = live
    mod._streamer.view_w, mod._streamer.view_h = mod.DESKTOP_LAYOUT_MIN_W, 0
    mod._streamer._vp_owner = ""
    mod._streamer._vp_seen.clear()
    client.set_cookie("op_stage", "; DROP TABLE")
    assert client.get("/operator").status_code == 200
    assert mod._streamer.view_h == 0


# operator.html / operator_demo.html both `{% extends "_base.html" %}` — that base
# is provided by the parent host-app app in production, not by the blueprint's
# own template folder. Mounted standalone here it's missing, so supply a minimal
# stand-in so the page routes render (we assert status/headers, not markup).
_STUB_BASE = ("<!doctype html><title>{% block title %}{% endblock %}</title>"
              "{% block content %}{% endblock %}")


def test_streamer_defaults_to_soft_zoom_and_desktop_width() -> None:
    # zoom default is 0.8 — two notches under neutral (the owner 2026-07-19). The
    # real "fits the screen" lever is view_w, which reflows the layout without
    # crossing responsive mobile breakpoints; zoom fine-tunes on top.
    s = OV._Streamer()
    assert s.zoom == 0.8
    assert s.view_w == 1280
    assert OV._VIEW_FOLLOW is True


def test_view_metrics_tolerate_scrollbar_shaved_width() -> None:
    # cssLayoutViewport EXCLUDES the ~15px scrollbar, so a scrollable page under
    # the forced 1280 width reads ~1012 CSS — 12px under the 1024 floor. A hard
    # floor made the per-frame mismatch check fail forever and _force_desktop_page
    # clear+apply-reflowed the REAL window at frame rate (the 2026-07-21 Windows
    # GUI strobe). Scrollbar-shaved widths must PASS; genuinely-collapsed mobile
    # viewports must still fail.
    s = OV._Streamer()
    assert s._matches_view_metrics(1012, 592) is True    # scrollbar-shaved
    assert s._matches_view_metrics(1024, 949) is True    # exact floor
    assert s._matches_view_metrics(988, 592) is True     # BOTH bars @125% (films-table strobe 2026-07-22)
    assert s._matches_view_metrics(976, 592) is True     # 4 quanta down — live probe catch, second films-table strobe
    assert s._matches_view_metrics(960, 592) is True     # edge of allowance
    assert s._matches_view_metrics(959, 592) is False    # below allowance
    assert s._matches_view_metrics(700, 592) is False    # top of the collapsed-emulation cluster
    assert s._matches_view_metrics(655, 593) is False    # stale collapsed emulation
    assert OV.DESKTOP_CSS_MIN_W - OV.SCROLLBAR_CSS_ALLOW == 960


def test_pdf_pages_resize_the_real_window_not_emulation() -> None:
    # Chrome's PDF viewer ignores setDeviceMetricsOverride (applies clean, no
    # layout/frame change — live-proven on the DS-11 PDF 2026-07-22: target
    # 1280x1074 applied, frame stayed 1024x859, "auto-resize not working").
    # PDF tabs must go through Browser.setWindowBounds instead.
    src = Path(OV.__file__).read_text(encoding="utf-8")
    apply_start = src.index("async def _apply_view_metrics")
    apply_src = src[apply_start:src.index("\n    async def ", apply_start + 10)]
    assert 'url.endswith(".pdf")' in apply_src
    assert "Browser.getWindowForTarget" in apply_src
    assert "Browser.setWindowBounds" in apply_src
    # web tabs keep emulation (it overrides window size for web content)
    assert "Emulation.setDeviceMetricsOverride" in apply_src


def test_model_picker_preserves_selected_label_width_for_caret_position() -> None:
    root = Path(__file__).resolve().parents[1]
    css = (root / "static/operator.css").read_text(encoding="utf-8")
    js = (root / "static/js/operator.js").read_text(encoding="utf-8")

    # dynamic caret: fitMini sizes #op-model to the selected name and pins that
    # width (flex:0 0 auto + min-width) so the name is never ellipsized; the CSS
    # no longer forces flex-shrink on it (which clipped "Sonnet 5" -> "Sonne…")
    assert "#op-model { min-width: 0; }" in css
    assert "#op-effort { flex: 0 1 auto; min-width: 0; max-width: 42%; }" in css
    assert "sel.style.flex = '0 0 auto';" in js
    assert "sel.style.minWidth = px;" in js


def test_native_select_click_uses_in_page_overlay() -> None:
    # native <select> popups are OS-drawn and unreachable over CDP; a click on
    # one renders an in-page, CDP-clickable option overlay instead (the owner
    # 2026-07-21, regressed when clicks moved to raw Input.dispatchMouseEvent).
    src = (Path(__file__).resolve().parents[1] / "operator_view.py").read_text(encoding="utf-8")
    assert "_SELECT_SHIM_JS" in src
    assert "async def _maybe_open_select" in src
    # only fires for click_at (not dblclick/rclick) and short-circuits the raw click
    assert 'if kind == "click_at" and await self._maybe_open_select(p, x, y):' in src
    # the overlay sets the value + fires change, and closes itself
    assert "sel.selectedIndex = i;" in src
    assert "new Event('change', {bubbles:true})" in src


def test_code_block_scroll_forwards_to_chat_log() -> None:
    # code blocks stole wheel/touch scroll ("sticks on a code block", the owner
    # 2026-07-21). The fix wires each <pre> directly (capture phase) + a
    # MutationObserver for future ones, forwarding vertical deltas to the log
    # unless the pre has its OWN scroll. Bubbling-delegate approach is gone —
    # it missed when the event never reached the log.
    js = (Path(__file__).resolve().parents[1] / "static/js/operator.js").read_text(encoding="utf-8")
    assert "function _wirePre(pre)" in js
    assert "capture: true" in js
    assert "new MutationObserver(_wireAllPres)" in js
    # both axes handled on the pre itself
    assert "pre.addEventListener('wheel'" in js
    assert "pre.addEventListener('touchmove'" in js
    # forwards to the log, not the pre
    assert "log.scrollTop += e.deltaY" in js


def test_ios_page_zoom_remains_available_over_operator_stage_and_input() -> None:
    root = Path(__file__).resolve().parents[1]
    static = root / "static"
    css = (static / "operator.css").read_text(encoding="utf-8")
    js = (static / "js/operator.js").read_text(encoding="utf-8")
    template = (root / "templates/operator.html").read_text(encoding="utf-8")

    stage_start = css.index(".op-stage {")
    stage_rule = css[stage_start:css.index("}", stage_start)]
    assert "touch-action: pinch-zoom;" in stage_rule
    assert "touch-action: none;" not in css
    assert ".op-lp { touch-action: pan-y pinch-zoom;" in css
    assert "user-scalable=no" not in js
    assert "maximum-scale=1" not in js
    touchstart = js[js.index("stage.addEventListener('touchstart'"):]
    touchstart = touchstart[:touchstart.index("stage.addEventListener('touchmove'")]
    assert "e.preventDefault()" not in touchstart

    ios_text_rule = css[css.index("@supports (-webkit-touch-callout: none)"):]
    ios_text_rule = ios_text_rule[:ios_text_rule.index("}\n  }")]
    assert "@media (pointer: coarse)" in ios_text_rule
    assert "#op textarea" in ios_text_rule
    assert '#op input[type="text"]' in ios_text_rule
    assert '#op input[type="url"]' not in ios_text_rule
    assert "font-size: 16px !important;" in ios_text_rule
    assert '<input type="url" class="op-url" id="op-url"' in template
    assert ".op-url { font-size: 0.66rem; }" in css


def test_ios_input_anti_zoom_tracks_picker_and_single_shrinks_placeholder() -> None:
    # "Message Operator size got bumped way down again and misaligned" (the owner
    # 2026-07-22, second occurrence). Mechanism: the composer computes 16px
    # (anti-focus-zoom) and is painted down by transform: scale(--op-ipt). The
    # regression was the ::placeholder keeping its OWN 0.74·--chat-scale
    # font-size inside the transformed element — shrunk twice (font-size, then
    # transform) to ~74% of the model picker, with the short line box riding the
    # top of the phantom-inflated row. This test pins the whole contract:
    root = Path(__file__).resolve().parents[1]
    css = (root / "static/operator.css").read_text(encoding="utf-8")
    js = (root / "static/js/operator.js").read_text(encoding="utf-8")

    blk_start = css.index("#op-input anti-zoom")
    blk = css[blk_start:blk_start + 3200]
    # 1) --op-ipt mirrors the picker's font formula (min(0.74·scale, 0.96)) —
    #    a pinned literal would drift from the scaling picker on A−/A+.
    assert "--op-ipt: min(calc(0.74 * var(--chat-scale)), 0.96);" in blk
    assert ".op-mini { font-size: min(calc(0.74rem * var(--chat-scale)), 0.96rem) !important; }" in css
    # 2) computed 16px (the anti-zoom threshold) painted back via the transform,
    #    with the width widened by the inverse so wrap points match.
    assert "font-size: 16px !important;" in blk
    assert "width: calc(100% / var(--op-ipt));" in blk
    assert "transform: scale(var(--op-ipt));" in blk
    #    display:block, or the inline-level textarea rides the wrap's text
    #    baseline and the line-box strut leaves ~6px dead space under the text
    #    (the "not centered, too high" half of the regression).
    assert "display: block;" in blk
    # 3) the placeholder inherits the 16px inside the block so the transform is
    #    the ONLY shrink (kills the double-shrink that caused the regression).
    assert "#op-input.op-input::placeholder { font-size: inherit; }" in blk
    # 4) idle phantom compensation: the (1−scale) layout excess is handed back
    #    so the one-line row isn't inflated (the "misaligned" half of the bug)…
    assert "margin-bottom: calc((var(--op-ipt) - 1) * (1.35em + 0.4rem));" in blk
    # …and autoGrow takes over live, measuring the real paint scale.
    assert "const paintScale = input.offsetWidth" in js
    assert "input.style.marginBottom = -(layoutHeight * (1 - paintScale)) + 'px';" in js
    # 5) iPad-width variant tracks the desktop 0.77rem source-order block.
    assert "--op-ipt: min(calc(0.77 * var(--chat-scale)), 0.96);" in blk


def test_splash_category_pills_never_clip_the_first_pill_at_low_zoom() -> None:
    # At low browser zoom the pill row (Browse … Saved) overflows its width;
    # plain `justify-content: center` centers the overflow and pushes the first
    # pill (Browse) past the flex-START edge, which overflow-scroll can never
    # reach (scrollLeft can't go negative) — so Browse is permanently clipped
    # and Saved clips off the right (the owner 2026-07-23). `safe center` falls back
    # to flex-start on overflow, so the row only ever spills off the scrollable
    # END. Pin it in both templates' CSS.
    root = Path(__file__).resolve().parents[1]
    for path in ("static/operator.css", "templates/operator_demo.html"):
        css = (root / path).read_text(encoding="utf-8")
        rule = css[css.index(".op-lp-cats {"):]
        rule = rule[:rule.index("}")]
        assert "justify-content: safe center;" in rule
        assert "justify-content: center;" not in rule   # the buggy value is gone
        assert "overflow-x: auto;" in rule               # still a scrollable row


def test_no_text_input_focus_zooms_on_ios() -> None:
    # "Tapping on the finish-up textbox zooms iOS in, eliminate this for all
    # text inputs on operator" (the owner 2026-07-23). iOS focus-zooms any editable
    # computing < 16px. Every operator text input must either compute 16px
    # (roomy controls) or paint-scale from 16px (compact ones) — none may keep
    # a raw sub-16px font on a coarse pointer. This pins BOTH escape hatches so
    # a future carve-out can't silently re-open the zoom.
    root = Path(__file__).resolve().parents[1]
    for tpl in ("static/operator.css", "templates/operator_demo.html"):
        css = (root / tpl).read_text(encoding="utf-8")
        ios = css[css.index("@supports (-webkit-touch-callout: none)"):]
        # the finish-up box — the specific complaint — is NO LONGER excluded
        # from the zoom-kill: the old `:not(.op-finish-input)` on the BLANKET
        # selector remains (it gets the transform instead), but it must now have
        # its own paint-scale rule (computes 16px, scaled back).
        fin = ios[ios.index(".op-finish-input {"):]
        fin = fin[:fin.index("}")]
        assert "font-size: 16px !important;" in fin
        assert "transform: scale(var(--op-sec));" in fin
        assert "width: calc(100% / var(--op-sec));" in fin
        # the save-modal fields + pill input are no longer carved out of the
        # blanket 16px (they used to ACCEPT the zoom): the pill input gets an
        # explicit 16px, and the modal text inputs fall into the blanket
        # selector now that :not(.op-nt-input) is gone from it.
        # Property, not formatting: the pill field still computes at 16px so
        # iOS never focus-zooms. It gained a paint-scale on 2026-07-31 (it was
        # rendering at a raw 16px next to 0.68rem labels), so the rule is no
        # longer a one-liner — pinning the literal line broke on a change that
        # kept the zoom kill perfectly intact.
        pill = ios[ios.index(".op-nt-pill-in {"):]
        pill = pill[:pill.index("}") + 1]
        assert "font-size: 16px !important;" in pill
        blanket = ios[ios.index("#op textarea:not(#op-input)"):]
        blanket = blanket[:blanket.index("{")]
        assert ":not(.op-nt-input)" not in blanket   # modal inputs now covered
        assert ":not(.op-nt-pill-in)" not in ios[ios.index("#op input[type=\"text\"]"):
                                                  ios.index("#op input[type=\"text\"]") + 120]


def test_slash_palette_removed_saved_tasks_live_on_launchpad() -> None:
    # The "/" saved-task palette stopped working; removed outright (the owner
    # 2026-07-22). Saved tasks remain reachable via launchpad cards + the save
    # modal — this pins both the removal and the survivors.
    root = Path(__file__).resolve().parents[1]
    js = (root / "static/js/operator.js").read_text(encoding="utf-8")
    css = (root / "static/operator.css").read_text(encoding="utf-8")
    live = (root / "templates/operator.html").read_text(encoding="utf-8")
    demo = (root / "templates/operator_demo.html").read_text(encoding="utf-8")

    for blob in (js, live, demo):
        assert 'id="op-pal"' not in blob
        assert "_opPalKeydown" not in blob
    assert ".op-pal {" not in css and ".op-pal {" not in demo
    assert "type / for saved tasks" not in css        # composer empty-state tip gone too
    # survivors: the shared runner the launchpad cards dispatch through, the
    # save modal, and its veil positioning (nearly lost in the removal sweep —
    # .op-veil sat adjacent to the palette CSS but belongs to the modal).
    assert "window._opRunSavedTask = async function(t){" in js
    assert 'id="op-nt-veil"' in live
    for blob in (css, demo):
        assert ".op-veil { position: fixed; inset: 0;" in blob


def test_splash_is_the_initial_html_boot_surface() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates/operator.html").read_text(encoding="utf-8")
    css = (root / "static/operator.css").read_text(encoding="utf-8")
    js = (root / "static/js/operator.js").read_text(encoding="utf-8")

    assert '<div class="op op-booting" id="op"' in template
    splash_open = template[template.index('<div class="op-lp op-lp-collapsed" id="op-lp"') :]
    splash_open = splash_open[:splash_open.index(">") + 1]
    assert "hidden" not in splash_open
    # the COLLAPSED assembly ships in the markup (1.0.26): initLaunchpad() runs
    # post-paint, so an expanded first paint flashed the tabs/grid on refresh.
    demo = (root / "templates/operator_demo.html").read_text(encoding="utf-8")
    assert '<div class="op-lp op-lp-collapsed" id="op-lp"' in demo
    assert ".op.op-booting .op-lp { display: flex !important; }" in css
    assert ".op.op-booting .op-urlbar" in css
    assert "classList.remove('op-booting')" in js


def test_operator_full_bleed_does_not_strip_the_shared_header_gutter() -> None:
    """The cockpit may span the viewport; host navigation may not."""
    root = Path(__file__).resolve().parents[1]
    css = (root / "static/operator.css").read_text(encoding="utf-8")

    assert "body.op-locked header.site" in css
    assert "width: min(calc(100% - 3rem), 1100px);" in css
    assert "margin-left: auto; margin-right: auto;" in css


def test_cross_origin_refusal_renders_as_a_warning_notice() -> None:
    """The CSRF guard's user-facing refusal must carry the red warning mark."""
    root = Path(__file__).resolve().parents[1]
    js = (root / "static/js/operator.js").read_text(encoding="utf-8")
    start = js.index("const CROSS_ORIGIN_MUTATION_ERROR")
    log_res = js[start:js.index("// Manual steering fires", start)]

    assert "CROSS_ORIGIN_MUTATION_ERROR" in log_res
    assert "cross-origin Operator mutation refused" in log_res
    assert "warningIcon()" in log_res


def test_release_version_surfaces_stay_in_sync() -> None:
    root = Path(__file__).resolve().parents[1]
    live = (root / "templates/operator.html").read_text(encoding="utf-8")
    demo = (root / "templates/operator_demo.html").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert re.fullmatch(r"\d+\.\d+\.\d+", OV.OP_VERSION)
    assert OV.OP_VERSION == "1.1.0"
    assert f"v{OV.OP_VERSION}" in readme
    assert '<span class="op-ver">{{ OP_VERSION }}</span>' in live
    assert '<span class="op-lp-mark-ver">v{{ OP_VERSION }}</span>' in live
    assert not re.search(r'class="op-ver">\d', live), "hardcoded version is back"
    assert '<span class="op-ver">{{ OP_VERSION }} demo</span>' in demo


def test_example_library_is_large_varied_and_site_backed() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "static/js/operator.js").read_text(encoding="utf-8")
    pool = js[js.index("const _LP_EXAMPLE_POOL") : js.index("function _lpBuild")]
    labels = js[js.index("const _SITE_LABELS") : js.index("const _LP_ROTATE_HOURS")]

    names = re.findall(r"\{ name: '([^']+)'", pool)
    sites = re.findall(r"sites: \['([^']+)'\]", pool)
    categories = re.findall(
        r"category: '(delivery|local|shopping|travel|research|media)'", pool)

    # doubled 2026-07-22 (the owner: category ↻ was a no-op at ~6 per category)
    assert 165 <= len(names) <= 220
    assert len(names) == len(set(names))
    assert len(sites) == len(names)
    assert len(sites) == len(set(sites))
    assert all(f"'{site}':" in labels for site in sites)
    assert all(categories.count(category) >= 12
               for category in ("delivery", "local", "shopping",
                                "travel", "research", "media"))
    assert set(categories) == {
        "delivery", "local", "shopping", "travel", "research", "media"
    }


def _build_app(demo: bool, *, interactive: bool = False):
    """Reload operator_view under the requested DEMO env, mount its blueprint on
    a throwaway Flask app, return (app, module). Reloading rebinds the module's
    DEMO constant + re-decorates the routes; it does NOT re-import operator_agent
    (operator_view only does `import operator_agent`), so a runner patched on
    OA.runner stays patched across the reload.

    NOTE: DEMO is a module-level global the routes read at request time, so only
    ONE flavor can be live at a time — a test must not hold a demo AND a live app
    simultaneously (the later reload wins for BOTH clients). Hence separate
    single-mode tests rather than dual-fixture ones."""
    if demo:
        os.environ["OPERATOR_DEMO"] = "1"
    else:
        os.environ.pop("OPERATOR_DEMO", None)
    if demo and interactive:
        os.environ["OPERATOR_UNSAFE_DEMO_INTERACTIVE"] = "1"
    else:
        os.environ.pop("OPERATOR_UNSAFE_DEMO_INTERACTIVE", None)
    mod = importlib.reload(OV)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mod.bp)
    # inject the stub _base.html alongside the blueprint's real templates
    app.jinja_loader = ChoiceLoader([app.jinja_loader,
                                     DictLoader({"_base.html": _STUB_BASE})])
    return app, mod


@pytest.fixture
def live():
    """Live-cockpit app (OPERATOR_DEMO unset). Restores module to live after."""
    app, mod = _build_app(demo=False)
    yield app.test_client(), mod
    _build_app(demo=False)   # leave the shared module in live mode for the next test


@pytest.fixture
def demo():
    """Explicit local-only interactive demo used by legacy behavior tests."""
    app, mod = _build_app(demo=True, interactive=True)
    yield app.test_client(), mod
    _build_app(demo=False)   # always restore live so we don't leak DEMO into siblings


@pytest.fixture
def public_demo():
    """The Internet-facing default: visible shell, inert runtime boundary."""
    app, mod = _build_app(demo=True, interactive=False)
    yield app.test_client(), mod
    _build_app(demo=False)


@pytest.mark.parametrize("method,path", [
    ("GET", "/operator/frame"),
    ("GET", "/operator/status"),
    ("GET", "/operator/agent"),
    ("GET", "/operator/driver-status"),
    ("GET", "/operator/shot/guess.png"),
    ("POST", "/operator/steer"),
    ("POST", "/operator/dispatch"),
    ("POST", "/operator/tasks"),
    ("DELETE", "/operator/tasks/example"),
])
def test_public_demo_is_read_only_by_default(public_demo, method, path):
    client, _ = public_demo
    response = client.open(path, method=method, json={})
    assert response.status_code == 403
    assert response.get_json()["error"] == "the public Operator demo is read-only"
    assert response.headers["X-Operator-Demo"] == "read-only"


def test_public_demo_page_is_static_and_security_headered(public_demo):
    client, _ = public_demo
    response = client.get("/operator")
    assert response.status_code == 200
    assert 'data-operator-interactive="0"' in response.get_data(as_text=True)
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_demo_never_reads_shared_action_log(demo, monkeypatch, tmp_path):
    _, mod = demo
    private_log = tmp_path / "operator-events.ndjson"
    private_log.write_text('{"bot":"private","detail":"private"}\n', encoding="utf-8")
    monkeypatch.setattr(mod, "_EVENT_LOG", str(private_log))
    assert mod._recent_events() == []


# ── runner / streamer fakes ──────────────────────────────────────────────────

class FakeRunner:
    """Stand-in for operator_agent.runner. Records start() calls so dispatch
    tests can assert the exact (bot, task, model, effort, demo) it was handed,
    and never launches a real agent."""
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []          # list of (args, kwargs) for start()
        self.stopped = False
        self.reset_bot = None
        self.stop_conversation_id = None
        self.snapshot_conversation_id = None

    def start(self, bot, task, model="", effort="", demo=False,
              surface="browser", real_ok=False, conversation_id=None):
        self.calls.append({"bot": bot, "task": task, "model": model,
                           "effort": effort, "demo": demo,
                           "surface": surface, "real_ok": real_ok,
                           "conversation_id": conversation_id})
        if self.ok:
            return {"ok": True, "bot": bot, "pid": 4242}
        return {"ok": False, "error": "already running"}

    def is_running(self):
        return False

    def stop(self, conversation_id=None):
        self.stopped = True
        self.stop_conversation_id = conversation_id
        return {"ok": True, "stopped": True}

    def reset_session(self, bot="", conversation_id=None):
        self.reset_bot = bot
        return {"ok": True, "bot": bot}

    def snapshot(self, since_ts=0.0, conversation_id=None):
        self.snapshot_conversation_id = conversation_id
        return {"state": "idle", "messages": [], "since": since_ts}


class FakeStreamer:
    """Stand-in for _Streamer. Records run_action() payloads (for the steer
    whitelist test) and never attaches to Chrome. Only the surface the routes
    touch is implemented."""
    def __init__(self):
        self.status = "idle"
        self.detail = ""
        self.frame = None
        self.frame_id = 0
        self.frame_ts = 0.0
        self.cur_url = ""
        self.vw = 0
        self.vh = 0
        self.last_view = 0.0
        self.last_click = (0.0, 0.0, 0.0)
        self._user_closed = False
        self.actions = []           # every dict passed to run_action
        self.tabs = []
        self.ready_error = None
        self.attached = True

    # routes call these — all inert
    def ensure_running(self):
        pass

    def vp_note_pull(self, cid):
        pass

    def _ensure_chrome_alive(self, relaunch=False):
        pass

    def require_ready(self, timeout=None):
        return self.ready_error

    def has_attached_page(self):
        return self.attached

    def run_action(self, action):
        self.actions.append(action)
        return {"ok": True, "url": "https://example.test", "echo": action}

    def list_tabs(self):
        return self.tabs

    def switch_tab(self, idx):
        return {"ok": True, "idx": idx}

    def close_tab(self, idx):
        return {"ok": True, "idx": idx}

    def new_tab(self):
        return {"ok": True}


@pytest.fixture
def fake_runner(monkeypatch):
    fr = FakeRunner()
    monkeypatch.setattr(OA, "runner", fr)
    return fr


@pytest.fixture
def fake_streamer(monkeypatch):
    """Swap the module-level _streamer singleton the routes reference. Patched on
    the live-imported OV; _build_app reloads OV, so tests that need this must
    patch AFTER the app fixture has reloaded — see _patch_streamer()."""
    return FakeStreamer()


def _patch_streamer(monkeypatch, mod, fs):
    monkeypatch.setattr(mod, "_streamer", fs)


# ═══════════════════════════════════════════════════════════════════════════
# 1. DEMO-mode gating — the public-demo / live-cockpit security boundary
# ═══════════════════════════════════════════════════════════════════════════

# Saved-task routes are live in BOTH flavors (the owner 2026-07-09) — the demo runs
# them against a demo-scoped store (OPERATOR_TASKS_PATH) and fails closed (404)
# if that env is missing, so a visitor can never reach the owner's store.
TASK_ROUTES = [
    ("GET", "/operator/tasks"),
    ("POST", "/operator/tasks"),
    ("POST", "/operator/tasks/somewhere/run"),
    ("DELETE", "/operator/tasks/somewhere"),
]


@pytest.mark.parametrize("method,path", TASK_ROUTES)
def test_saved_task_routes_reachable_in_demo(demo, fake_runner, monkeypatch, method, path):
    # the owner 2026-07-09: the demo gets saved tasks too — against a demo-scoped
    # store (OPERATOR_TASKS_PATH), never the owner's. Reachable = no flat gate.
    client, mod = demo
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    monkeypatch.setenv("OPERATOR_TASKS_PATH", "/tmp/op-demo-tasks-test.json")
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "load_tasks", lambda: {})
    monkeypatch.setattr(OT, "get_task", lambda slug: None)
    monkeypatch.setattr(OT, "delete_task", lambda slug: False)
    monkeypatch.setattr(OT, "save_task", lambda d: (None, "empty name"))
    resp = client.open(path, method=method, json={})
    assert resp.status_code != 404 or (resp.get_json() or {}).get("error") != "not available"


def test_demo_task_run_applies_dispatch_lock(demo, fake_runner, monkeypatch):
    # a stored bundle can't smuggle a privileged bot/model/effort past the demo
    client, mod = demo
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    monkeypatch.setenv("OPERATOR_TASKS_PATH", "/tmp/op-demo-tasks-test.json")
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "get_task", lambda slug: {
        "prompt": "do the thing", "bot": "claude-a", "model": "opus",
        "effort": "max", "sites": [], "start_url": ""})
    monkeypatch.setattr(OT, "sites_preamble", lambda sites: "")
    monkeypatch.setattr(OT, "mark_run", lambda slug: None)
    resp = client.post("/operator/tasks/sneaky/run", json={})
    assert resp.status_code == 200
    call = fake_runner.calls[0]
    assert call["bot"] == "gemma"
    assert call["model"] == "gemini-3.7-flash-low"   # off-list stored model → default
    assert call["effort"] == ""
    assert call["demo"] is True


def test_demo_task_save_strips_bot_and_schedule(demo, monkeypatch):
    client, mod = demo
    monkeypatch.setenv("OPERATOR_TASKS_PATH", "/tmp/op-demo-tasks-test.json")
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "load_tasks", lambda: {})
    seen = {}
    monkeypatch.setattr(OT, "save_task", lambda d: (seen.update(d), ("x", None))[1])
    resp = client.post("/operator/tasks", json={
        "name": "N", "task": "P", "bot": "claude-a", "schedule": "0 9 * * *"})
    assert resp.status_code == 200
    assert seen["bot"] == ""          # dead field in demo (forced at run)
    assert seen["schedule"] == ""     # scheduler never runs on a public instance


def test_demo_task_store_cap(demo, monkeypatch):
    client, mod = demo
    monkeypatch.setenv("OPERATOR_TASKS_PATH", "/tmp/op-demo-tasks-test.json")
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    full = {f"t{i}": {"name": f"t{i}", "prompt": "p"} for i in range(mod.DEMO_TASKS_MAX)}
    monkeypatch.setattr(OT, "load_tasks", lambda: full)
    monkeypatch.setattr(OT, "save_task", lambda d: ("t0", None))
    # NEW task at cap → refused
    resp = client.post("/operator/tasks", json={"name": "new", "task": "p"})
    assert resp.status_code == 400
    assert "limit" in resp.get_json()["error"]
    # update-in-place of an EXISTING slug at cap → still fine
    resp = client.post("/operator/tasks", json={"slug": "t0", "name": "t0", "task": "p2"})
    assert resp.status_code == 200


@pytest.mark.parametrize("method,path", TASK_ROUTES)
def test_saved_task_routes_reachable_in_live(live, fake_runner, monkeypatch, method, path):
    client, mod = live
    fs = FakeStreamer()
    _patch_streamer(monkeypatch, mod, fs)
    # stub the tasks store so /run + list + delete don't hit the real ~/.cache file
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "load_tasks", lambda: {})
    monkeypatch.setattr(OT, "get_task", lambda slug: None)
    monkeypatch.setattr(OT, "delete_task", lambda slug: False)
    monkeypatch.setattr(OT, "save_task", lambda d: (None, "empty name"))
    resp = client.open(path, method=method, json={})
    # reachable = NOT the demo 404-refusal. It may legitimately 400/404-on-missing,
    # but never the flat demo gate. Assert it did real work (hit the store branch).
    assert resp.status_code != 404 or (resp.get_json() or {}).get("error") != "not available"


def test_unseen_is_zero_in_demo(demo):
    client, _ = demo
    assert client.get("/operator/unseen").get_json() == {"count": 0}


def test_drivers_generic_in_demo(demo):
    client, _ = demo
    dj = client.get("/operator/drivers").get_json()
    assert dj == {"drivers": [{"key": "bot", "label": "bot"}]}   # no squad names leak


def test_drivers_named_in_live(live):
    client, _ = live
    keys = {d["key"] for d in client.get("/operator/drivers").get_json()["drivers"]}
    assert "claude-a" in keys                                       # real drivers exposed


def test_models_locked_to_two_model_choice_in_demo(demo):
    # the owner 2026-07-09: Flash 3.6 Low default (first = picker default) + Sonnet
    # 4.6 as the only alt; tier baked into the value, effort control hidden.
    client, _ = demo
    assert client.get("/operator/models").get_json()["models"] == [
        {"value": "gemini-3.7-flash-low", "label": "3.7 Flash"},
        {"value": "Claude Sonnet 4.6 (Thinking)", "label": "Sonnet 4.6"},
    ]


def test_models_multiple_in_live(live):
    client, _ = live
    assert len(client.get("/operator/models").get_json()["models"]) >= 2


# ═══════════════════════════════════════════════════════════════════════════
# 2. /operator/dispatch — the agent-launch route
# ═══════════════════════════════════════════════════════════════════════════

def test_dispatch_live_calls_runner_with_expected_args(live, fake_runner, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    resp = client.post("/operator/dispatch", json={
        "bot": "claude-a", "task": "check the news", "model": "opus", "effort": "high"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert len(fake_runner.calls) == 1
    call = fake_runner.calls[0]
    assert call["bot"] == "claude-a"
    assert call["task"] == "check the news"
    assert call["model"] == "opus"
    assert call["effort"] == "high"
    assert call["demo"] is False        # live path never passes demo=True
    assert call["surface"] == "browser"  # Track C: default surface rides along
    assert call["real_ok"] is False


def test_dispatch_demo_forces_gemma_and_default_model_and_demo_true(demo, fake_runner, monkeypatch):
    client, mod = demo
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    # client TRIES to inject a privileged bot/model/effort; demo must ignore it
    resp = client.post("/operator/dispatch", json={
        "bot": "claude-a", "task": "do it", "model": "opus", "effort": "high"})
    assert resp.status_code == 200
    call = fake_runner.calls[0]
    assert call["bot"] == "gemma"                          # forced, client bot ignored
    assert call["model"] == "gemini-3.7-flash-low"       # off-list model → default
    assert call["effort"] == ""                            # lock owns effort (tier in model string)
    assert call["demo"] is True                            # strips squad context


def test_dispatch_demo_honors_sonnet_alt(demo, fake_runner, monkeypatch):
    # the ONE allowed alternative model passes through; everything else stays locked
    client, mod = demo
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    resp = client.post("/operator/dispatch", json={
        "bot": "claude-a", "task": "do it",
        "model": "Claude Sonnet 4.6 (Thinking)", "effort": "max"})
    assert resp.status_code == 200
    call = fake_runner.calls[0]
    assert call["bot"] == "gemma"
    assert call["model"] == "Claude Sonnet 4.6 (Thinking)"  # allowlisted alt honored
    assert call["effort"] == ""                            # injected effort still ignored
    assert call["demo"] is True


def test_dispatch_demo_sandbox_surface_forces_sonnet(demo, fake_runner, monkeypatch):
    # Flash has no computer-use tools (the owner 2026-07-09): a demo sandbox run
    # always gets Sonnet, even when the visitor picked (or defaulted to) Flash.
    client, mod = demo
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    resp = client.post("/operator/dispatch", json={
        "bot": "x", "task": "open the editor", "model": "gemini-3.7-flash-low",
        "surface": "desktop-sandbox"})
    assert resp.status_code == 200
    call = fake_runner.calls[0]
    assert call["model"] == "Claude Sonnet 4.6 (Thinking)"
    assert call["surface"] == "desktop-sandbox"
    assert call["demo"] is True


def test_dispatch_empty_task_rejected_cleanly(live, fake_runner, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    resp = client.post("/operator/dispatch", json={"bot": "claude-a", "task": "   "})
    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": "empty task"}
    assert fake_runner.calls == []          # runner never invoked on a bad body


def test_dispatch_malformed_body_is_400_not_500(live, fake_runner, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    # garbage body / wrong content-type → get_json(silent=True) falls back to form,
    # task ends up empty → clean 400, NOT a 500.
    resp = client.post("/operator/dispatch", data="not json at all",
                       content_type="text/plain")
    assert resp.status_code == 400
    assert fake_runner.calls == []


def test_dispatch_runner_conflict_returns_409(live, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    monkeypatch.setattr(OA, "runner", FakeRunner(ok=False))
    resp = client.post("/operator/dispatch", json={"bot": "claude-a", "task": "go"})
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False


def test_dispatch_forwards_conversation_id_to_runner(live, fake_runner, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    resp = client.post("/operator/dispatch", json={
        "bot": "claude-a", "task": "go", "conversation_id": "conv-a"})
    assert resp.status_code == 200
    assert fake_runner.calls[0]["conversation_id"] == "conv-a"


def test_dispatch_refuses_to_start_agent_without_a_usable_browser(
        live, fake_runner, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    fs.ready_error = "browser connection timed out"
    _patch_streamer(monkeypatch, mod, fs)

    resp = client.post("/operator/dispatch", json={
        "bot": "gemma", "task": "find flights", "surface": "browser"})

    assert resp.status_code == 503
    assert resp.get_json() == {
        "ok": False, "error": "browser connection timed out"}
    assert fake_runner.calls == [], "a blind browser run must never begin"


def test_desktop_dispatch_does_not_require_the_browser(
        demo, fake_runner, monkeypatch):
    client, mod = demo
    fs = FakeStreamer()
    fs.ready_error = "browser is down"
    _patch_streamer(monkeypatch, mod, fs)

    resp = client.post("/operator/dispatch", json={
        "bot": "gemma", "task": "open the editor",
        "surface": "desktop-sandbox"})

    assert resp.status_code == 200
    assert len(fake_runner.calls) == 1
    assert fake_runner.calls[0]["surface"] == "desktop-sandbox"


# ═══════════════════════════════════════════════════════════════════════════
# 3. /operator/steer — the action whitelist (regression guard for the
#    silently-dropped-field bug class: dx/dy scroll deltas, x0..y1 drag coords)
# ═══════════════════════════════════════════════════════════════════════════

# For each action kind, the fields _do_action actually consumes must survive the
# route's manual whitelist into the dict handed to run_action. If any are dropped
# the action silently degrades (the historical scroll-up / drag-to-origin bugs).
STEER_CASES = [
    # (posted body, kind, {field: expected_value_in_action})
    ("scroll dy", {"kind": "scroll", "dy": -600, "dx": 0}, "scroll",
     {"dy": -600, "dx": 0}),
    ("scroll dx", {"kind": "scroll", "dx": 120}, "scroll", {"dx": 120}),
    ("drag coords", {"kind": "drag", "x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.9},
     "drag", {"x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.9}),
    ("click_at xy", {"kind": "click_at", "x": 0.5, "y": 0.6}, "click_at",
     {"x": 0.5, "y": 0.6}),
    ("click_at count", {"kind": "click_at", "x": 0.5, "y": 0.6, "count": 3},
     "click_at", {"count": 3}),
    ("rclick_at xy", {"kind": "rclick_at", "x": 0.3, "y": 0.4}, "rclick_at",
     {"x": 0.3, "y": 0.4}),
    ("mousedown_at xy", {"kind": "mousedown_at", "x": 0.1, "y": 0.1},
     "mousedown_at", {"x": 0.1, "y": 0.1}),
    ("goto value", {"kind": "goto", "value": "example.test"}, "goto",
     {"value": "example.test"}),
    ("type value", {"kind": "type", "value": "hello"}, "type", {"value": "hello"}),
    ("key value", {"kind": "key", "value": "Enter"}, "key", {"value": "Enter"}),
    ("stage size force",
     {"kind": "stage_size", "value": "704x615", "cid": "tab-a", "force": True},
     "stage_size", {"cid": "tab-a", "force": True}),
]


@pytest.mark.parametrize("label,body,kind,expected", STEER_CASES,
                         ids=[c[0] for c in STEER_CASES])
def test_steer_whitelist_preserves_fields(live, monkeypatch, label, body, kind, expected):
    client, mod = live
    fs = FakeStreamer()
    _patch_streamer(monkeypatch, mod, fs)
    resp = client.post("/operator/steer", json=body)
    assert resp.status_code == 200
    assert len(fs.actions) == 1
    action = fs.actions[0]
    assert action["kind"] == kind
    for field, val in expected.items():
        assert field in action, f"{kind}: field '{field}' was dropped by the whitelist"
        assert action[field] == val, f"{kind}: field '{field}' mangled ({action[field]!r} != {val!r})"


def test_steer_scroll_delta_defaults_to_none_not_zero(live, monkeypatch):
    """The load-bearing subtlety behind the wheel-up bug: when NO dx/dy is posted,
    they must arrive as None (so _do_action distinguishes 'no delta → keyword'
    from 'a real 0 delta'). A 0 default would silently break keyword scrolls."""
    client, mod = live
    fs = FakeStreamer()
    _patch_streamer(monkeypatch, mod, fs)
    client.post("/operator/steer", json={"kind": "scroll", "value": "up"})
    action = fs.actions[0]
    assert action["dx"] is None
    assert action["dy"] is None
    assert action["value"] == "up"


def test_steer_missing_kind_is_400(live, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    resp = client.post("/operator/steer", json={"value": "x"})
    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": "missing action kind"}


def test_steer_accepts_form_encoded_body(live, monkeypatch):
    """Route reads get_json(silent=True) OR request.form — a form POST must work."""
    client, mod = live
    fs = FakeStreamer()
    _patch_streamer(monkeypatch, mod, fs)
    resp = client.post("/operator/steer", data={"kind": "type", "value": "hi"})
    assert resp.status_code == 200
    assert fs.actions[0]["kind"] == "type"
    assert fs.actions[0]["value"] == "hi"


def test_extensions_action_reaches_private_browser(live, monkeypatch):
    live_client, live_mod = live
    live_streamer = FakeStreamer()
    _patch_streamer(monkeypatch, live_mod, live_streamer)
    live_resp = live_client.post("/operator/steer", json={"kind": "extensions"})

    assert live_resp.status_code == 200
    assert live_streamer.actions[0]["kind"] == "extensions"


def test_extensions_action_is_blocked_in_demo(demo, monkeypatch):
    demo_client, demo_mod = demo
    demo_streamer = FakeStreamer()
    _patch_streamer(monkeypatch, demo_mod, demo_streamer)
    demo_resp = demo_client.post("/operator/steer", json={"kind": "extensions"})

    assert demo_resp.status_code == 403
    assert demo_streamer.actions == []


# ═══════════════════════════════════════════════════════════════════════════
# 4. /operator/status + /operator/tasks — happy path + missing-state resilience
# ═══════════════════════════════════════════════════════════════════════════

def test_status_happy_path_shape(live, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    fs.status = "live"
    fs.cur_url = "https://example.test"
    fs.vw, fs.vh = 1280, 800
    _patch_streamer(monkeypatch, mod, fs)
    # keep clear_unseen from touching the real schedule module/file
    import operator_schedule as OS
    monkeypatch.setattr(OS, "clear_unseen", lambda: None)
    resp = client.get("/operator/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"status", "detail", "has_frame", "vw", "vh", "url", "click",
                         "surface", "browser_up"}   # browser_up: Chrome reachability, separate from the feed (the owner 2026-08-02)
    assert body["status"] == "live"
    assert body["url"] == "https://example.test"
    assert body["vw"] == 1280 and body["vh"] == 800
    assert body["has_frame"] is False        # no frame set → not fresh


def test_status_never_calls_a_detached_stale_frame_live(live, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    fs.status = "live"
    fs.attached = False
    fs.frame = b"stale pixels"
    fs.frame_ts = time.monotonic()
    _patch_streamer(monkeypatch, mod, fs)

    body = client.get("/operator/status").get_json()

    assert body["status"] == "connecting"
    assert body["has_frame"] is False


def test_status_survives_schedule_module_blowup(live, monkeypatch):
    """clear_unseen() is wrapped in try/except — a broken schedule import must not
    500 the status poll (the cockpit polls this every second)."""
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    import operator_schedule as OS
    def boom():
        raise RuntimeError("schedule store gone")
    monkeypatch.setattr(OS, "clear_unseen", boom)
    resp = client.get("/operator/status")
    assert resp.status_code == 200            # swallowed, not surfaced


def test_tasks_list_happy_path(live, monkeypatch):
    client, mod = live
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "load_tasks", lambda: {
        "morning": {"name": "Morning", "prompt": "do", "created": 1, "last_run": None},
    })
    resp = client.get("/operator/tasks")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert len(body["tasks"]) == 1
    t = body["tasks"][0]
    assert t["slug"] == "morning"
    assert t["name"] == "Morning"
    # _task_public projects a stable shape
    assert set(t) >= {"slug", "name", "prompt", "sites", "bot", "model",
                      "effort", "start_url", "schedule", "created", "last_run"}


def test_tasks_list_empty_when_store_missing(live, monkeypatch):
    client, mod = live
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "load_tasks", lambda: {})   # store.py already no-raises
    resp = client.get("/operator/tasks")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "tasks": []}


def test_tasks_post_rejects_empty_name(live, monkeypatch):
    client, mod = live
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "save_task", lambda d: (None, "empty name"))
    resp = client.post("/operator/tasks", json={"name": "", "task": "x"})
    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": "empty name"}


def test_task_run_missing_slug_is_404_not_500(live, fake_runner, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "get_task", lambda slug: None)      # no such task
    resp = client.post("/operator/tasks/ghost/run", json={})
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "no such task"}
    assert fake_runner.calls == []


def test_task_run_dispatches_and_marks_run(live, fake_runner, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    _patch_streamer(monkeypatch, mod, fs)
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "get_task", lambda slug: {
        "prompt": "read the filings", "bot": "claude-a", "model": "opus",
        "effort": "high", "sites": [], "start_url": ""})
    monkeypatch.setattr(OT, "sites_preamble", lambda sites: "")
    marked = []
    monkeypatch.setattr(OT, "mark_run", lambda slug: marked.append(slug))
    resp = client.post("/operator/tasks/deepdive/run", json={})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert fake_runner.calls[0]["task"] == "read the filings"
    assert fake_runner.calls[0]["bot"] == "claude-a"
    assert marked == ["deepdive"]           # last_run stamped only on ok dispatch


def test_saved_task_run_cannot_bypass_browser_readiness(
        live, fake_runner, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    fs.ready_error = "browser connection failed"
    _patch_streamer(monkeypatch, mod, fs)
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "get_task", lambda slug: {
        "prompt": "read the filings", "bot": "claude-a", "model": "opus",
        "effort": "high", "sites": [], "start_url": ""})

    resp = client.post("/operator/tasks/deepdive/run", json={})

    assert resp.status_code == 503
    assert resp.get_json() == {
        "ok": False, "error": "browser connection failed"}
    assert fake_runner.calls == []


# ═══════════════════════════════════════════════════════════════════════════
# 5. tabs + agent passthrough routes (thin wrappers → streamer/runner)
# ═══════════════════════════════════════════════════════════════════════════

def test_tabs_route_returns_streamer_snapshot(live, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    fs.tabs = [{"i": 0, "title": "Google", "url": "https://g.test", "active": True}]
    _patch_streamer(monkeypatch, mod, fs)
    resp = client.get("/operator/tabs")
    assert resp.status_code == 200
    assert resp.get_json()["tabs"] == fs.tabs


def test_tab_switch_close_new_are_post_only(live, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    assert client.get("/operator/tab/0").status_code == 405          # GET not allowed
    assert client.post("/operator/tab/0").get_json() == {"ok": True, "idx": 0}
    assert client.post("/operator/tab/1/close").get_json() == {"ok": True, "idx": 1}
    assert client.post("/operator/tab/new").get_json() == {"ok": True}


def test_agent_stop_and_reset_delegate_to_runner(live, fake_runner):
    client, _ = live
    assert client.post("/operator/agent/stop").get_json()["stopped"] is True
    assert fake_runner.stopped is True
    client.post("/operator/agent/reset", json={"bot": "claude-a"})
    assert fake_runner.reset_bot == "claude-a"


def test_agent_snapshot_parses_since_and_tolerates_garbage(live, fake_runner):
    client, _ = live
    ok = client.get("/operator/agent?since=1234.5").get_json()
    assert ok["since"] == 1234.5
    # non-numeric `since` must not 500 — the route coerces to 0.0
    bad = client.get("/operator/agent?since=notanumber")
    assert bad.status_code == 200
    assert bad.get_json()["since"] == 0.0


def test_agent_stop_and_snapshot_target_the_requested_conversation(live, fake_runner):
    client, _ = live
    client.post("/operator/agent/stop", json={"conversation_id": "conv-a"})
    assert fake_runner.stop_conversation_id == "conv-a"
    client.get("/operator/agent?since=3&conversation_id=conv-b")
    assert fake_runner.snapshot_conversation_id == "conv-b"


# ═══════════════════════════════════════════════════════════════════════════
# 6. pure-ish helpers — event tail + driver detection (no Chrome, filesystem seam)
# ═══════════════════════════════════════════════════════════════════════════

def test_recent_events_reads_ndjson_tail(live, monkeypatch, tmp_path):
    _, mod = live
    log = tmp_path / "events.ndjson"
    log.write_text('{"bot":"claude-a","action":"click","ts":1}\n'
                   'bad line that is not json\n'
                   '{"bot":"gpt","action":"type","ts":2}\n', encoding="utf-8")
    monkeypatch.setattr(mod, "_EVENT_LOG", str(log))
    evs = mod._recent_events(40)
    assert len(evs) == 2                     # the malformed line is skipped
    assert evs[-1]["bot"] == "gpt"


def test_recent_events_missing_file_returns_empty(live, monkeypatch, tmp_path):
    _, mod = live
    monkeypatch.setattr(mod, "_EVENT_LOG", str(tmp_path / "nope.ndjson"))
    assert mod._recent_events() == []        # OSError swallowed → []


def test_current_driver_within_window(live, monkeypatch):
    import time
    _, mod = live
    now = time.time()
    monkeypatch.setattr(mod, "_recent_events",
                        lambda n=8: [{"bot": "claude-a", "action": "click",
                                      "detail": "the button", "ts": now}])
    drv = mod._current_driver(window_s=12.0)
    assert drv["bot"] == "claude-a"
    assert drv["action"] == "click"


def test_current_driver_stale_is_none(live, monkeypatch):
    _, mod = live
    monkeypatch.setattr(mod, "_recent_events",
                        lambda n=8: [{"bot": "claude-a", "action": "click", "ts": 0}])
    assert mod._current_driver(window_s=12.0) is None   # ancient ts → nobody driving


def test_current_driver_masks_bot_name_in_demo(demo, monkeypatch):
    import time
    _, mod = demo
    now = time.time()
    monkeypatch.setattr(mod, "_recent_events",
                        lambda n=8: [{"bot": "claude-a", "action": "click", "ts": now}])
    drv = mod._current_driver()
    assert drv["bot"] == "assistant"        # never leak the real squad bot name


def test_assistant_text_extracts_from_content_blocks(live):
    _, mod = live
    # string content
    assert mod._assistant_text({"message": {"content": "hi there"}}) == "hi there"
    # block-list content — only text blocks, joined
    msg = {"message": {"content": [
        {"type": "text", "text": "first"},
        {"type": "tool_use", "name": "x"},
        {"type": "text", "text": "second"}]}}
    assert mod._assistant_text(msg) == "first second"
    # no content → empty
    assert mod._assistant_text({"message": {}}) == ""


def test_iso_epoch_parses_and_defaults_zero(live):
    _, mod = live
    assert mod._iso_epoch("2026-07-02T06:15:00+00:00") > 0
    assert mod._iso_epoch("") == 0.0
    assert mod._iso_epoch("garbage") == 0.0
    assert mod._iso_epoch(None) == 0.0


def test_slug_matches_claude_project_dir_convention(live):
    _, mod = live
    # abspath with / . _ all collapsed to '-'
    s = mod._slug("/home/user/agents/claude-a")
    assert s == "-home-user-agents-claude-a"
    assert "_" not in mod._slug("/tmp/a_b/c.d")


def test_shot_route_rejects_traversal_and_bad_ext(live, monkeypatch):
    client, mod = live
    # path traversal / non-basename → 404 before touching the filesystem
    assert client.get("/operator/shot/..%2f..%2fetc%2fpasswd").status_code == 404
    assert client.get("/operator/shot/notanimage.txt").status_code == 404
    assert client.get("/operator/shot/.hidden.png").status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 7. simple page routes
# ═══════════════════════════════════════════════════════════════════════════

def test_cockpit_redirects_to_operator(live):
    client, _ = live
    resp = client.get("/cockpit")
    assert resp.status_code in (301, 302, 308)
    assert "/operator" in resp.headers["Location"]


def test_operator_page_renders_and_is_no_store(live):
    client, _ = live
    resp = client.get("/operator")
    assert resp.status_code == 200
    assert "no-store" in resp.headers.get("Cache-Control", "")
    assert 'data-kind="extensions"' in resp.get_data(as_text=True)


def test_demo_page_does_not_expose_extensions_launcher(demo):
    client, _ = demo
    resp = client.get("/operator")
    assert resp.status_code == 200
    assert 'data-kind="extensions"' not in resp.get_data(as_text=True)


def test_standalone_flag_hides_the_squad_store_chrome(live, monkeypatch):
    """operator-fam: OPERATOR_STANDALONE=1 renders the cockpit full-viewport
    (header.site hidden); the host-app-mounted cockpit keeps the site nav."""
    import operator_view as OV
    client, _ = live
    html = client.get("/operator").get_data(as_text=True)
    assert "header.site { display: none; }" not in html   # default: nav stays
    monkeypatch.setattr(OV, "STANDALONE", True)
    html = client.get("/operator").get_data(as_text=True)
    assert "header.site { display: none; }" in html
    assert "calc(100dvh - 22px)" in html


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ── /operator/frame — the pull half of the feed (anti buffer-bloat) ─────────
def test_frame_serves_placeholder_before_first_capture(live, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    mod._active_surface["name"] = "browser"
    r = client.get("/operator/frame")
    assert r.status_code == 200
    assert r.mimetype == "image/jpeg"
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Operator-Frame"] == "placeholder"
    assert r.headers["X-Operator-Frame-ID"] == "browser:0"


def test_frame_serves_newest_live_frame(live, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    fs.frame = b"\xff\xd8fake-jpeg-bytes\xff\xd9"
    fs.frame_id = 7
    _patch_streamer(monkeypatch, mod, fs)
    mod._active_surface["name"] = "browser"
    r = client.get("/operator/frame")
    assert r.status_code == 200
    assert r.headers["X-Operator-Frame"] == "live"
    assert r.headers["X-Operator-Frame-ID"] == "browser:7"
    assert r.data == fs.frame
    assert fs.last_view > 0     # a pull counts as viewing (feeds the idle-stop)


def test_frame_returns_no_jpeg_when_client_already_has_current_frame(live, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    fs.frame = b"\xff\xd8same-frame\xff\xd9"
    fs.frame_id = 4
    _patch_streamer(monkeypatch, mod, fs)
    mod._active_surface["name"] = "browser"

    r = client.get("/operator/frame?since=browser:4&wait=0")

    assert r.status_code == 204
    assert r.data == b""
    assert r.headers["X-Operator-Frame-ID"] == "browser:4"
    assert fs.last_view > 0


def test_waiting_frame_pull_wakes_as_soon_as_pixels_change(live, monkeypatch):
    client, mod = live
    fs = FakeStreamer()
    fs.frame = b"\xff\xd8old\xff\xd9"
    fs.frame_id = 2
    _patch_streamer(monkeypatch, mod, fs)
    mod._active_surface["name"] = "browser"

    def publish() -> None:
        time.sleep(0.04)
        fs.frame = b"\xff\xd8new\xff\xd9"
        fs.frame_id = 3
        fs.frame_ts = time.monotonic()

    threading.Thread(target=publish, daemon=True).start()
    started = time.monotonic()
    r = client.get("/operator/frame?since=browser:2&wait=300")
    elapsed = time.monotonic() - started

    assert r.status_code == 200
    assert r.data == fs.frame
    assert r.headers["X-Operator-Frame-ID"] == "browser:3"
    assert elapsed < 0.2


def test_client_long_polls_by_frame_id_and_handles_empty_updates():
    js = (Path(__file__).parents[1] / "static/js/operator.js").read_text()
    assert "wait=900" in js
    assert "X-Operator-Frame-ID" in js
    assert "r.status === 204" in js


# ── feed self-heal: cycle a decayed sandbox stream ───────────────────────────
def test_stream_decay_decision():
    F = OV._DesktopFeed
    # young stream: never judged, even at zero frames
    assert F._stream_decayed(0, 6.0, age_s=3.0) is False
    # short window: not enough evidence
    assert F._stream_decayed(0, 2.0, age_s=60.0) is False
    # aged + sagging (measured decay: ~0.7fps vs configured 10) → cycle
    assert F._stream_decayed(3, 6.0, age_s=60.0) is True
    # aged + healthy (10fps) → keep
    assert F._stream_decayed(50, 5.0, age_s=600.0) is False
    # boundary: exactly the floor is NOT decayed (strict less-than)
    assert F._stream_decayed(20, 5.0, age_s=60.0) is False


def test_task_run_with_vars_requires_values(live, fake_runner, monkeypatch):
    """1.0.13: a {{variable}} task can't run unfilled — 400 names the vars so
    the client can prefill the composer instead."""
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "get_task", lambda slug: {
        "prompt": "check {{ticker}} price on {{site}}", "bot": "claude-a",
        "model": "", "effort": "", "sites": [], "start_url": ""})
    monkeypatch.setattr(OT, "sites_preamble", lambda sites: "")
    resp = client.post("/operator/tasks/tpl/run", json={})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["vars"] == ["ticker", "site"]
    assert "variable" in body["error"]
    assert fake_runner.calls == []
    # partial fill → still 400, only the missing one named
    resp = client.post("/operator/tasks/tpl/run",
                       json={"vars": {"ticker": "AAPL"}})
    assert resp.status_code == 400
    assert resp.get_json()["vars"] == ["site"]


def test_task_run_fills_vars_into_prompt(live, fake_runner, monkeypatch):
    client, mod = live
    _patch_streamer(monkeypatch, mod, FakeStreamer())
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "get_task", lambda slug: {
        "prompt": "check {{ticker}} price", "bot": "claude-a",
        "model": "", "effort": "", "sites": [], "start_url": ""})
    monkeypatch.setattr(OT, "sites_preamble", lambda sites: "")
    monkeypatch.setattr(OT, "mark_run", lambda slug: None)
    resp = client.post("/operator/tasks/tpl/run",
                       json={"vars": {"ticker": "AAPL"}})
    assert resp.status_code == 200
    task = fake_runner.calls[0]["task"]
    assert "AAPL" in task and "{{" not in task


def test_tasks_list_exposes_vars(live, fake_runner, monkeypatch):
    client, mod = live
    import operator_tasks as OT
    monkeypatch.setattr(mod, "operator_tasks_store", OT)
    monkeypatch.setattr(OT, "load_tasks", lambda: {
        "tpl": {"name": "tpl", "prompt": "check {{ticker}}"}})
    rows = client.get("/operator/tasks").get_json()["tasks"]
    assert rows[0]["vars"] == ["ticker"]


def test_splash_mark_is_a_top_right_status_ring_not_a_hero_logo() -> None:
    # the owner 2026-07-24: the mark does NOT sit above the wordmark. It lives in the
    # splash's top-right control row (X at 1.1rem, theme at 4rem, mark at 6.9rem)
    # at the same 32px box, and doubles as a backend-health readout.
    root = Path(__file__).resolve().parents[1]
    css = (root / "static/operator.css").read_text(encoding="utf-8")
    html = (root / "templates/operator.html").read_text(encoding="utf-8")

    # markup order: the mark precedes the theme toggle, i.e. it is a sibling in
    # the control row rather than a child of the hero block.
    assert html.index('id="op-lp-mark"') < html.index('id="op-lp-theme"')
    assert html.index('id="op-lp-mark"') < html.index('class="op-lp-hero"')

    rule = css[css.index(".op-lp-mark { position: fixed"):]
    rule = rule[: rule.index("}")]
    assert "right: 6.9rem" in rule                      # third slot in the row
    assert "--op-lp-control-size: 32px;" in css          # shared with theme + X
    assert "width: var(--op-lp-control-size);" in rule
    assert "height: var(--op-lp-control-size);" in rule

    # health states ride #op's existing data-state — no separate status plumbing
    assert '.op[data-state="error"] .op-lp-mark-sweep' in css
    assert '.op[data-state="hung"] .op-lp-mark-sweep' in css
    assert ".op.op-signal-lost .op-lp-mark-sweep" in css
    assert "@keyframes op-mark-alarm" in css

    # hover greet, and it may only run once the ring has settled
    assert "@keyframes op-mark-greet" in css
    # class-gated (NOT :hover-driven): mouse-off must let the turn complete
    assert ".op-lp-mark.op-greeting .op-lp-mark-glyph" in css


def test_mark_animations_complete_whole_rotations() -> None:
    # the owner 2026-07-24: the hover greet does a FULL 360, and the chat spinner
    # does two full rotations per cycle (720 total) — not the half-turns both
    # shipped with. Landing on whole turns also keeps the 2-fold-symmetric
    # glyph's rest pose identical to its start pose.
    css = (Path(__file__).resolve().parents[1] / "static/operator.css").read_text(encoding="utf-8")

    def keyframes(name: str) -> str:
        start = css.index(f"@keyframes {name} {{")
        depth = 0
        for i in range(css.index("{", start), len(css)):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    return css[start:i + 1]
        raise AssertionError(f"unterminated @keyframes {name}")

    # Split tracks (the owner 2026-07-27 "halt in the middle"): the turn is one
    # continuous from->to whole revolution on the `rotate` property; the swell
    # rides `scale` so easing the breath can't stall the rotation.
    greet = keyframes("op-mark-greet-turn")
    assert "rotate: 360deg" in greet          # lands on a WHOLE turn
    assert "215deg" not in greet              # no mid-stop — that was the stall
    swell = keyframes("op-mark-greet-swell")
    assert "scale: 1.14" in swell and "rotate" not in swell

    halt = keyframes("op-mark-halt")
    assert "rotate(360deg)" in halt and "rotate(720deg)" in halt
    assert "rotate(180deg)" not in halt


def test_mobile_type_overrides_win_the_cascade() -> None:
    # A @media query adds NO specificity, so a mobile `.op-lp-title` written
    # ABOVE the base `.op-lp-title` ties on specificity and loses on source
    # order — the phone silently keeps rendering desktop sizes. That shipped
    # broken for two rounds (the owner 2026-07-26: "still not fixed"). Every mobile
    # override of these must sit after the base rule it overrides.
    css = (Path(__file__).resolve().parents[1] / "static/operator.css").read_text(encoding="utf-8")
    # anchor on the base rules by their actual font declarations, which are
    # unique, rather than on indentation (2-space also matches inside 4-space).
    # .op-lp-name deliberately has NO mobile override any more — the cards are
    # meant to look identical to desktop (the owner 2026-07-26) — so only the section
    # heading, the one thing that genuinely doesn't fit, is checked here.
    for selector, base_decl in (
        (".op-lp-title", ".op-lp-title { font: 700 calc(1.35rem"),
    ):
        base = css.index(base_decl)
        override = css.rindex(f"    {selector} {{ font-size:")
        assert override > base, (
            f"{selector}'s mobile override sits before its base rule and will "
            f"lose the cascade (base @{base}, override @{override})"
        )


def test_status_card_spinner_stays_the_green_ring() -> None:
    # the owner 2026-07-24: ONLY the chat working-spinner takes the new mark. The
    # status card's ring is untouched, or the two read as the same control.
    css = (Path(__file__).resolve().parents[1] / "static/operator.css").read_text(encoding="utf-8")
    assert "animation: op-spin .85s cubic-bezier(.5,.15,.5,.85) infinite;" in css
    card = css[css.index(".op-spinner {"):]
    card = card[: card.index("}")]
    assert "op-ico-hooks" not in card and "op-mark-halt" not in card


def test_chat_task_spinner_is_the_operator_mark_and_yields_to_the_checkmark() -> None:
    # The conic-gradient arc (and its z-order bug — the ::after track painted at
    # the same annulus and swallowed the ::before arc, leaving a bare gray ring)
    # is gone: the spinner is the Operator mark itself, an SVG in .ico's markup.
    # The finished states are still ::before content, so the mark MUST be hidden
    # at busy=0 or the checkmark would render on top of a live-looking logo.
    root = Path(__file__).resolve().parents[1]
    css = (root / "static/operator.css").read_text(encoding="utf-8")
    js = (root / "static/js/operator.js").read_text(encoding="utf-8")

    assert "conic-gradient(from 0deg, var(--fg-2) 0deg 84deg" not in css
    # the mark ships in the task-head markup, not as a pseudo-element
    assert "const MARK_SVG" in js
    assert "ico.innerHTML = MARK_SVG" in js
    for cls in ("op-ico-mark", "op-ico-ring", "op-ico-hooks"):
        assert cls in js and cls in css

    hide = '.op-task[data-busy="0"] .op-task-head .ico .op-ico-mark'
    hide_rule = css[css.index(hide):css.index("}", css.index(hide))]
    assert "display: none" in hide_rule
    # and the finished marks still exist to take its place — geometric masked
    # strokes now, not font glyphs (the owner 2026-07-26)
    done = css.index('.op-task[data-busy="0"] .op-task-head .ico::before')
    done_rule = css[done:css.index("}", done)]
    assert "mask: url(\"data:image/svg+xml" in done_rule
    assert "background: var(--live)" in done_rule

    # only the busy state animates
    assert '.op-task[data-busy="1"] .op-task-head .ico .op-ico-hooks' in css
    assert "@keyframes op-mark-halt" in css

    # No outer ring on this one (the owner 2026-07-25) — the splash badge wears the
    # ring; the spinner is bare hooks that fill the box.
    ring = css[css.index(".op-task-head .ico .op-ico-ring {"):]
    assert "display: none" in ring[: ring.index("}")]
    assert 'viewBox="6.4 6.4 11.2 11.2"' in js          # tight to the hooks

    # The breath moved ONTO the hooks when the ring was dropped — without it the
    # holds sat dead still. It must swell above rest somewhere in the cycle.
    halt = css[css.index("@keyframes op-mark-halt {"):]
    halt = halt[: halt.index("\n  @") if "\n  @" in halt else len(halt)]
    assert "scale(1.08)" in halt

    # and the icon scales with the verb rather than a fixed px (alignment)
    ico = css[css.index(".op-task-head .ico { width:"):]
    assert "em" in ico[: ico.index("}")]


# ── collapsed-viewport repair scoring (the owner 2026-07-29) ──────────────────────
# The flight recorder caught the real shape of "the viewport went super narrow":
# ten `vp-walk 1024->651` events in one session, i.e. the layout viewport
# FLIPPING between healthy (1024) and collapsed (651) rather than walking. The
# repair that exists for the collapsed band needed REPAIR_AFTER_MISSES
# CONSECUTIVE misses, and _gate_misses is zeroed by any healthy read — so an
# alternating reading never reached the threshold and nothing ever corrected it.

def test_collapse_score_fires_on_two_consecutive_collapsed_reads():
    s = 0
    for _ in range(2):
        s = OV.collapse_score(s, gate_ok=False, css_w=651)
    assert s >= OV.COLLAPSE_REPAIR_AT


def test_collapse_score_ignores_a_lone_transient():
    """One collapsed frame mid-navigation must never reflow the real window —
    that reflow is the visible strobe the persistence gate was built to stop."""
    s = OV.collapse_score(0, gate_ok=False, css_w=651)
    assert s < OV.COLLAPSE_REPAIR_AT
    for _ in range(4):
        s = OV.collapse_score(s, gate_ok=True, css_w=1024)
    assert s == 0


def test_collapse_score_still_catches_a_flip_flop():
    """The actual bug: alternating collapsed/healthy reads. A decay that merely
    zeroed the counter let this run forever."""
    s, fired = 0, False
    for i in range(8):
        s = OV.collapse_score(s, gate_ok=bool(i % 2), css_w=1024 if i % 2 else 651)
        if s >= OV.COLLAPSE_REPAIR_AT:
            fired = True
            break
    assert fired, "a flip-flopping viewport must eventually repair"


def test_collapse_score_ignores_the_ambiguous_scrollbar_zone():
    """Reads between the hard floor and the gate floor are scrollbar arithmetic,
    not collapsed emulation — they log (vp-walk) and must not accumulate."""
    s = 0
    for _ in range(6):
        s = OV.collapse_score(s, gate_ok=False, css_w=930)
    assert s == 0


def test_launchpad_mark_turns_once_when_it_settles() -> None:
    """the owner 2026-07-30: "when the green ring loading finishes and it becomes
    Ready ... do a spin animation".

    The settle already ran a decelerating lap on .op-lp-mark-spin, and nothing
    about it was broken — it was simply invisible. The settle closes the sweep
    into a FULL ring first, and a full ring is identical at every angle, so
    there was no way to see the turn. (The CSS says so in its own comment,
    which is why this is pinned rather than "fixed" in the spin rule.)

    The glyph is asymmetric, so it is the thing that can show a rotation. It
    must animate `rotate`, not `transform` — op-mark-settle owns transform for
    the scale, and a second transform animation would overwrite it rather than
    compose, dropping the cross-fade's scale.
    """
    root = Path(__file__).resolve().parents[1]
    css = (root / "static/operator.css").read_text(encoding="utf-8")

    rule = css[css.index(".op-mark-settled .op-lp-mark-glyph { opacity: 1;"):]
    rule = rule[:rule.index("}") + 1]
    assert "op-mark-settle " in rule, "the existing cross-fade must survive"
    assert "op-mark-glyph-turn" in rule
    assert "transform" not in rule, (
        "use the individual `rotate` property — a transform animation here "
        "overwrites op-mark-settle's scale instead of composing with it")

    kf = css[css.index("@keyframes op-mark-glyph-turn"):]
    kf = kf[:kf.index("}", kf.index("to")) + 1]
    assert "rotate: 0deg" in kf and "rotate: 360deg" in kf

    # the spin wrapper's own settle lap is left exactly as it was
    assert "animation: op-mark-spin-settle 1.15s cubic-bezier(.25,.8,.3,1) 1 forwards;" in css

    # reduced motion keeps the cross-fade but drops the turn. Anchor on the
    # block that silences the spinner — there are several reduced-motion
    # blocks and the nearest one after the keyframes is the error-sweep.
    rm = css[css.index(".op-lp-mark-spin { animation: none; }"):]
    rm = rm[:rm.index("}\n") + 400]
    assert ".op-mark-settled .op-lp-mark-glyph" in rm, \
        "reduced motion must re-state the glyph rule without the turn"
    assert "op-mark-glyph-turn" not in rm


def test_launchpad_mark_reports_connected_only_for_a_live_feed() -> None:
    """Renamed by the owner 2026-07-30. The tip is CSS `content`, driven off
    #op's state — there is no JS string to grep, which is why it gets a pin."""
    root = Path(__file__).resolve().parents[1]
    css = (root / "static/operator.css").read_text(encoding="utf-8")
    assert ('[data-state="live"].op-mark-settled '
            '.op-lp-mark-tip::before { content: "Connected"; }') in css
    assert '\n  .op-mark-settled .op-lp-mark-tip::before { content: "Connected"; }' not in css
    assert '[data-state="connecting"] .op-lp-mark-sweep' in css
    assert 'content: "Ready"' not in css


def _streamer_with_view(view_w, vw=1280, vh=1050):
    """A bare streamer stand-in — _usable_click_basis only touches these."""
    s = OV._Streamer.__new__(OV._Streamer)
    s.view_w, s.view_h = view_w, 0
    s.vw, s.vh = vw, vh
    return s


def test_a_collapsed_viewport_is_refused_as_a_click_basis() -> None:
    """Clicks are normalized 0..1 and multiplied by the CSS viewport, so the
    basis IS the pointer's calibration. _accept_viewport's floor is 320 —
    written to keep single-digit garbage out of the frame clip — and a
    collapsed-but-plausible read sails through it.

    The recorder caught `vp-walk 1024->651` twice on 2026-07-31 while the owner was
    reporting "clicks arent landing in the right place": a click aimed at the
    right edge would land at 651/1024 = 64% of the way across. The frame path
    refuses anything under REPAIR_HARD_FLOOR_W; this makes the click path
    agree."""
    s = _streamer_with_view(1024)
    assert s._usable_click_basis(1024, 900)          # healthy
    assert not s._usable_click_basis(651, 900)       # the logged collapse
    assert not s._usable_click_basis(320, 900)       # passes the OLD floor


def test_a_refused_click_basis_does_not_poison_the_cached_dims() -> None:
    """_accept_viewport writes self.vw/self.vh as a side effect, and those are
    the last-resort fallback in _viewport_dims. If a collapsed read reached it,
    rejecting the read still left the collapse cached for the next caller — so
    the floor check has to run FIRST."""
    s = _streamer_with_view(1024, vw=1024, vh=900)
    assert not s._usable_click_basis(651, 900)
    assert (s.vw, s.vh) == (1024, 900), "collapsed read must not be cached"


def test_click_basis_floor_never_exceeds_the_view_target() -> None:
    """A deliberately small target (a phone-sized stage) must not have every
    read rejected by an 800px floor built for desktop."""
    s = _streamer_with_view(420)
    assert s._usable_click_basis(420, 800)
    assert not s._usable_click_basis(300, 800)   # still under MIN_VIEWPORT_W


def test_click_basis_without_a_view_target_keeps_the_old_floor() -> None:
    """Before the first beacon there is no target to measure against; falling
    back to the permissive floor is better than refusing every click."""
    s = _streamer_with_view(0)
    assert s._usable_click_basis(651, 900)


def test_save_modal_single_line_fields_are_paint_scaled_like_the_textarea() -> None:
    """The save dialog's zoom-kill left its two SINGLE-LINE fields at a raw
    16px while the prompt textarea was painted back to 0.68rem — so the task
    name and the sites/tools field towered over their own labels (the owner
    2026-07-31: "placeholder too big in task name, Websites and tools").

    Measured on the iOS declarations injected into Chromium (the @supports
    gate is WebKit-only and this box has no WebKit runtime deps, so the GATE
    is not what's pinned here — the geometry inside it is):
        before   label 10.92 | name 16.00 | prompt 10.88 | sites 16.00
        after    label 10.92 | name 10.88 | prompt 10.88 | sites 10.88

    The chip field is a FLEX item and deliberately gets only the paint half of
    the trick — widening a flex child fights its basis, which is what the
    original "no transform (flex item)" note was right about.
    """
    root = Path(__file__).resolve().parents[1]
    css = (root / "static/operator.css").read_text(encoding="utf-8")

    gate = css.index("@supports (-webkit-touch-callout: none)")
    blk = css[gate:gate + 5200]

    name = blk[blk.index("input.op-nt-input {"):]
    name = name[:name.index("}") + 1]
    assert "font-size: 16px !important;" in name      # the zoom kill stays
    assert "transform: scale(var(--op-nts))" in name  # ...painted back down
    assert "width: calc(100% / var(--op-nts));" in name
    assert "margin-bottom:" in name, "must reclaim the phantom height"

    pill = blk[blk.index(".op-nt-pill-in {"):]
    pill = pill[:pill.index("}") + 1]
    assert "font-size: 16px !important;" in pill
    assert "transform: scale(0.68)" in pill
    assert "transform-origin: left center" in pill, \
        "the chip row centres its items; a top origin lifts the text off the line"
    assert "width:" not in pill and "flex:" not in pill, \
        "a flex child must not be widened — that fights its basis"


# ── browser-up probe: the launchpad must be able to tell a dead browser from an
# idle feed. /operator/status reports the STREAMER, which rests at 'idle' either
# way, so the mark settled into its healthy pose over a browser that was gone
# (the owner 2026-08-02). ─────────────────────────────────────────────────────────

def test_status_reports_browser_reachability_separately_from_the_feed(live, monkeypatch) -> None:
    client, mod = live
    monkeypatch.setattr(mod, "_cdp_up_cached", lambda: False)
    d = client.get("/operator/status").get_json()
    # the feed is idle (no streamer running in tests) yet the browser is down —
    # the whole point is that these two are allowed to disagree
    assert d["browser_up"] is False
    assert d["status"] != "error", "streamer state must be reported unchanged"


def test_cdp_probe_never_blocks_the_status_request(live, monkeypatch) -> None:
    """A WEDGED Chrome is exactly when _cdp_alive() burns its full timeout, so
    the probe must run off the request path."""
    client, mod = live
    mod._cdp_probe.update(up=None, ts=0.0, running=False)

    calls = {"n": 0}

    def _slow_probe():
        calls["n"] += 1
        raise AssertionError("probe must not run inline on the request thread")

    monkeypatch.setattr(mod._streamer, "_cdp_alive", _slow_probe)
    # First call kicks off a background probe and returns immediately with the
    # not-yet-known value; the assertion inside _slow_probe would only fire on
    # the worker thread, so the request itself must still succeed.
    d = client.get("/operator/status").get_json()
    assert d["browser_up"] is None, "unprobed state is None, not a guessed False"


def test_probe_is_cached_not_refired_every_poll(live, monkeypatch) -> None:
    client, mod = live
    import time as _t
    mod._cdp_probe.update(up=True, ts=_t.monotonic(), running=False)
    calls = {"n": 0}
    monkeypatch.setattr(mod._streamer, "_cdp_alive",
                        lambda: (calls.__setitem__("n", calls["n"] + 1), True)[1])
    for _ in range(5):
        client.get("/operator/status")
    assert calls["n"] == 0, "a fresh cache entry must serve every poll"


def test_streamer_attach_has_a_hard_timeout(monkeypatch) -> None:
    """A half-alive Chrome must become an error, not sit at Connecting forever."""
    streamer = OV._Streamer()

    async def never_attaches():
        await asyncio.sleep(1)

    async def inert_teardown():
        return None

    monkeypatch.setattr(OV, "BROWSER_ATTACH_TIMEOUT", 0.01)
    monkeypatch.setattr(streamer, "_attach", never_attaches)
    monkeypatch.setattr(streamer, "_teardown", inert_teardown)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(streamer._grab_loop())
