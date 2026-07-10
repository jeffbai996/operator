"""Tests for operator_agent.py's agy trajectory parsing — #37 (raw thinking text
must never become the final reply) and #40 (overthink-loop warning).

No flask/subprocess dependency at import time (operator_agent.py only pulls in
stdlib at module scope), so this imports the module directly. Run with:
  pytest test_operator_agent.py -q
"""
import json
import os
import tempfile

import pytest

import operator_agent as OA
import operator_agy


def make_runner():
    r = OA.AgentRunner()
    # _agy_parse_trajectory relies on per-run state that __init__ doesn't set
    # (only _run() does, right before a real run starts) — mirror that reset.
    r._agy_seen = set()
    r._agy_noprogress_streak = 0
    r._agy_loop_warned = False
    return r


def write_traj(steps):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    for s in steps:
        f.write(json.dumps(s) + "\n")
    f.close()
    return f.name


def planner_step(idx, thinking=None, tool_calls=None, content=None):
    o = {"source": "MODEL", "type": "PLANNER_RESPONSE", "step_index": idx}
    if thinking is not None:
        o["thinking"] = thinking
    if tool_calls is not None:
        o["tool_calls"] = tool_calls
    if content is not None:
        o["content"] = content
    return o


# ── #37: thinking text must be tagged role="thinking", never "assistant" ────

def test_thinking_only_step_is_tagged_thinking_not_assistant():
    r = make_runner()
    path = write_traj([planner_step(0, thinking="scanning the page for a login button")])
    try:
        got_answer = r._agy_parse_trajectory(path)
    finally:
        os.unlink(path)
    assert got_answer is False
    roles = [m["role"] for m in r.messages]
    assert "thinking" in roles
    assert "assistant" not in roles


def test_final_content_step_is_tagged_assistant():
    r = make_runner()
    path = write_traj([planner_step(0, thinking="wrapping up", content="Here's the answer.")])
    try:
        got_answer = r._agy_parse_trajectory(path)
    finally:
        os.unlink(path)
    assert got_answer is True
    assistant_msgs = [m for m in r.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["text"] == "Here's the answer."


def test_dead_file_link_stripped_to_label_only():
    r = make_runner()
    text = "see [trace.json](file:///home/user/.gemini/brain/trace.json) for detail"
    path = write_traj([planner_step(0, content=text)])
    try:
        r._agy_parse_trajectory(path)
    finally:
        os.unlink(path)
    assistant_msgs = [m for m in r.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert "file://" not in assistant_msgs[0]["text"]
    assert "trace.json" in assistant_msgs[0]["text"]


# ── #40: overthink-loop warning ──────────────────────────────────────────────

def test_noprogress_streak_resets_on_tool_call():
    r = make_runner()
    steps = [planner_step(i, thinking="hmm") for i in range(3)]
    steps.append(planner_step(3, thinking="ok acting now",
                               tool_calls=[{"name": "browser_click", "args": {}}]))
    path = write_traj(steps)
    try:
        r._agy_parse_trajectory(path)
    finally:
        os.unlink(path)
    assert r._agy_noprogress_streak == 0
    assert r._agy_loop_warned is False


def test_long_noprogress_streak_emits_one_warning():
    r = make_runner()
    n = OA.AgentRunner._AGY_LOOP_WARN_STREAK + 2
    steps = [planner_step(i, thinking=f"still thinking step {i}") for i in range(n)]
    path = write_traj(steps)
    try:
        r._agy_parse_trajectory(path)
    finally:
        os.unlink(path)
    assert r._agy_loop_warned is True
    warnings = [m for m in r.messages if m["role"] == "error" and "stuck in a loop" in m["text"]]
    assert len(warnings) == 1  # one-shot, even though the streak kept growing past threshold


def test_short_noprogress_streak_below_threshold_no_warning():
    r = make_runner()
    n = OA.AgentRunner._AGY_LOOP_WARN_STREAK - 1
    steps = [planner_step(i, thinking=f"thinking {i}") for i in range(n)]
    path = write_traj(steps)
    try:
        r._agy_parse_trajectory(path)
    finally:
        os.unlink(path)
    assert r._agy_loop_warned is False
    assert r._agy_noprogress_streak == n


# ── #40 (b): loop-breaking nudge — preventive preamble + reactive next-turn ──
#
# These append to tests/test_operator_agent.py. Contract:
#   Lever 1 (preventive): the agy prompt preamble carries a standing directive
#     telling the model to break out of pure-reasoning loops (act or answer).
#   Lever 2 (reactive): when a run trips the ≥6 no-progress streak, a one-shot
#     next-turn flag arms; the NEXT agy prompt prepends an explicit nudge and
#     the flag clears (consume-once). No trip → no flag → no nudge.


# ---- Lever 1: preventive preamble ----

def test_agy_preamble_contains_loop_break_directive():
    # The standing step-by-step directive block must include loop-breaking
    # guidance so every agy run is told to act-or-answer rather than re-reason.
    text = OA._AGY_STEPWISE_DIRECTIVE if hasattr(OA, "_AGY_STEPWISE_DIRECTIVE") else ""
    low = text.lower()
    assert "loop" in low or "in a row" in low or "re-describe" in low or "same reasoning" in low
    # it should point at the escape hatch: take an action OR give the answer
    assert "action" in low and ("answer" in low or "conclu" in low)


# ---- Lever 2: reactive next-turn flag ----

def test_loop_trip_arms_next_turn_nudge_flag():
    r = make_runner()
    r._agy_loop_nudge_pending = False
    n = OA.AgentRunner._AGY_LOOP_WARN_STREAK + 1
    steps = [planner_step(i, thinking=f"still thinking {i}") for i in range(n)]
    path = write_traj(steps)
    try:
        r._agy_parse_trajectory(path)
    finally:
        os.unlink(path)
    assert r._agy_loop_warned is True
    assert r._agy_loop_nudge_pending is True   # armed for next turn


def test_no_loop_leaves_nudge_flag_unarmed():
    r = make_runner()
    r._agy_loop_nudge_pending = False
    steps = [planner_step(0, thinking="think"),
             planner_step(1, tool_calls=[{"name": "browser_click", "args": {}}]),
             planner_step(2, content="done")]
    path = write_traj(steps)
    try:
        r._agy_parse_trajectory(path)
    finally:
        os.unlink(path)
    assert r._agy_loop_nudge_pending is False


def test_nudge_prepend_helper_consumes_flag_once():
    # _agy_apply_loop_nudge(task) → returns task with the nudge prepended when the
    # flag is armed, and CLEARS the flag so a subsequent call is a no-op.
    r = make_runner()
    r._agy_loop_nudge_pending = True
    out1 = r._agy_apply_loop_nudge("Book me a flight")
    assert out1 != "Book me a flight"            # nudge prepended
    assert "Book me a flight" in out1            # original task preserved
    low = out1.lower()
    assert "loop" in low or "last" in low or "re-describ" in low
    assert r._agy_loop_nudge_pending is False    # consumed
    # second call: flag cleared → task returned untouched
    out2 = r._agy_apply_loop_nudge("Book me a flight")
    assert out2 == "Book me a flight"


def test_nudge_helper_noop_when_flag_unset():
    r = make_runner()
    r._agy_loop_nudge_pending = False
    assert r._agy_apply_loop_nudge("do the thing") == "do the thing"


def test_nudge_flag_defaults_false_on_fresh_runner():
    # A brand-new runner (as __init__ leaves it) must not spuriously nudge.
    r = OA.AgentRunner()
    assert getattr(r, "_agy_loop_nudge_pending", False) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ─────────── agy conversation-id capture (threads --conversation) ───────────
# agy's -p print mode emits no session id, but each run creates
# <uuid>.db in its conversations dir — so the id is captured by
# set-differencing the dir across the run. Ambiguity (0 or 2+ new ids,
# e.g. a concurrent agy run elsewhere) must yield None: threading the
# WRONG conversation is far worse than running fresh.

def test_agy_conversation_ids_lists_db_stems(tmp_path):
    (tmp_path / "aaa-111.db").write_bytes(b"x")
    (tmp_path / "bbb-222.db").write_bytes(b"x")
    (tmp_path / "bbb-222.db-wal").write_bytes(b"x")  # sqlite sidecars ignored
    (tmp_path / "notes.txt").write_bytes(b"x")
    assert operator_agy.conversation_ids(str(tmp_path)) == {"aaa-111", "bbb-222"}


def test_agy_conversation_ids_missing_dir_is_empty():
    assert operator_agy.conversation_ids("/nonexistent/convs") == set()


def test_agy_new_conversation_single_diff_captured():
    before = {"aaa-111"}
    after = {"aaa-111", "ccc-333"}
    assert operator_agy.new_conversation(before, after) == "ccc-333"


def test_agy_new_conversation_no_diff_is_none():
    s = {"aaa-111"}
    assert operator_agy.new_conversation(s, s) is None


def test_agy_new_conversation_ambiguous_diff_is_none():
    """Two new dbs = a concurrent agy run raced us — do NOT guess."""
    before = {"aaa-111"}
    after = {"aaa-111", "ccc-333", "ddd-444"}
    assert operator_agy.new_conversation(before, after) is None
