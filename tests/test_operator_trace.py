"""1.0.8 R1 — direct unit tests for the trace-labeling subsystem.

These pure functions were only exercised indirectly (through _consume) before
the operator_trace extraction; this file pins their behavior down directly.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_operator_trace.py -q
"""
import operator_trace as OT


# ── action_label: browser tools ──────────────────────────────────────────────

def test_mcp_namespaced_tool_still_labels():
    # names arrive namespaced; failing to strip the prefix made the trace
    # show only "Thinking", never the click/nav steps
    assert OT.action_label("mcp__playwright__browser_navigate",
                           {"url": "https://example.com"}) == \
        ("Browsing", "https://example.com")


def test_coordinate_click_surfaces_the_coords():
    assert OT.action_label("browser_mouse_click_xy", {"x": 420, "y": 315}) == \
        ("Clicking", "(420, 315)")


def test_drag_shows_start_and_end():
    label, detail = OT.action_label(
        "browser_mouse_drag_xy", {"startX": 120, "startY": 80, "endX": 300, "endY": 240})
    assert label == "Dragging" and detail == "(120, 80) → (300, 240)"


def test_capitalcase_args_from_agy_still_surface_detail():
    # agy/Gemini sends CapitalCase keys where claude/codex send lowercase
    label, detail = OT.action_label("browser_type", {"Text": "hello world"})
    assert label == "Typing" and detail == "hello world"


def test_trivial_ref_detail_is_dropped():
    # an opaque auto-ref (e6) means nothing to the viewer — bare verb is better
    assert OT.action_label("browser_click", {"ref": "e6"}) == ("Clicking", "")


def test_wait_humanizes_the_duration():
    assert OT.action_label("browser_wait_for", {"time": 90}) == ("Waiting", "1m 30s")


# ── action_label: computer (desktop) multiplex tool ──────────────────────────

def test_computer_click_labels_by_action_arg():
    assert OT.action_label("computer", {"action": "left_click",
                                        "coordinate": [420, 315]}) == \
        ("Clicking", "(420, 315)")


def test_computer_type_shows_the_text():
    assert OT.action_label("computer", {"action": "type", "text": "hi"}) == \
        ("Typing", "hi")


def test_computer_unknown_action_still_named():
    label, detail = OT.action_label("computer", {"action": "warp_pointer"})
    assert label == "Using computer" and detail == "warp pointer"


# ── action_label: non-browser tools ──────────────────────────────────────────

def test_known_nonbrowser_tool_with_query_detail():
    assert OT.action_label("websearch", {"query": "test terms"}) == \
        ("Searching", "test terms")


def test_unknown_verb_noun_tool_gets_a_gerund():
    assert OT.action_label("fetch_messages", {}) == ("Fetching messages", "")


def test_unknown_tool_falls_back_to_code_chip():
    assert OT.action_label("frobnicate_widget", {}) == \
        ("Using `frobnicate_widget`", "")


def test_plumbing_tools_are_skipped():
    assert OT.action_label("toolsearch", {}) == ("", "")


def test_non_string_tool_is_safe():
    assert OT.action_label(None, {}) == ("", "")


# ── scrub_detail ─────────────────────────────────────────────────────────────

def test_pure_home_path_collapses_to_basename():
    assert OT.scrub_detail("/home/someone/.gemini/shot.png") == "shot.png"


def test_embedded_home_path_collapses_to_tilde():
    out = OT.scrub_detail("saved to /home/someone/.cache/computer-use/shot.png ok")
    assert "/home/someone" not in out and "shot.png" in out


def test_scrub_caps_length():
    assert len(OT.scrub_detail("x" * 500)) <= 120


# ── extract_handoff ──────────────────────────────────────────────────────────

def test_handoff_marker_stripped_and_reason_returned():
    assert OT.extract_handoff("done [[TAKE_CONTROL: captcha]]") == ("done", "captcha")


def test_handoff_marker_alone_leaves_empty_text():
    text, reason = OT.extract_handoff("[[TAKE_CONTROL: 2FA needed]]")
    assert text == "" and reason == "2FA needed"


def test_no_marker_returns_none_reason():
    assert OT.extract_handoff("all done") == ("all done", None)


# ── fmt_duration ─────────────────────────────────────────────────────────────

def test_fmt_duration_tiers():
    assert OT.fmt_duration(0.5) == "500ms"
    assert OT.fmt_duration(5) == "5s"
    assert OT.fmt_duration(90) == "1m 30s"
    assert OT.fmt_duration(120) == "2m"
    assert OT.fmt_duration(3661) == "1h 1m 1s"


# ── clean_gemma_text ─────────────────────────────────────────────────────────

def test_task_started_noise_is_stripped():
    assert "Task started" not in OT.clean_gemma_text("🛑 Task started: foo\nreal answer")


def test_unservable_local_image_becomes_a_note():
    out = OT.clean_gemma_text("look: ![shot](file:///nowhere/missing.png)")
    assert "file://" not in out and "took a screenshot" in out


def test_servable_screenshot_rewrites_to_shot_route(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPUTER_USE_OUTPUT_DIR", str(tmp_path))
    (tmp_path / "shot1.png").write_bytes(b"\x89PNG")
    out = OT.clean_gemma_text("![s](file://%s/shot1.png)" % tmp_path)
    assert "![s](operator/shot/shot1.png)" in out


def test_trailing_files_literal_is_stripped():
    assert OT.clean_gemma_text("answer\nfiles=['/tmp/a.png']").strip() == "answer"


def test_ascii_table_gets_fenced():
    out = OT.clean_gemma_text("| a | b |\n| 1 | 2 |")
    assert out.startswith("```")


def test_harmony_final_channel_extracted():
    raw = ("<|channel|>analysis<|message|>secret reasoning<|end|>"
           "<|channel|>final<|message|>the answer<|end|>")
    out = OT.clean_gemma_text(raw)
    assert out == "the answer"


# ── shot_dirs (R3: the single source of truth) ───────────────────────────────

def test_shot_dirs_env_override_and_bot_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPUTER_USE_OUTPUT_DIR", str(tmp_path))
    dirs = OT.shot_dirs()
    assert dirs[0] == str(tmp_path)
    assert any(d.endswith("/.operator-sessions/gpt") for d in dirs)


def test_view_and_trace_agree_on_shot_dirs():
    """R3: the /operator/shot route and the trace rewriter MUST resolve the
    same dir list or rewritten inline-image links 404."""
    import operator_view as OV
    assert OV._SHOT_DIRS == OT.shot_dirs()
