"""1.0.9 R5 — the extracted agy trajectory subsystem.

tests/fixtures/agy_trajectory.jsonl + agy_trajectory_expected.json were
captured through the PRE-refactor parser (operator_agent._agy_parse_trajectory)
— the replay here proves the extraction emits the identical message stream:
same roles, same labels, same handoff, same got_answer.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_operator_agy.py -q
"""
import json
import os
import time

import pytest

import operator_agy as AGY

FIXDIR = os.path.join(os.path.dirname(__file__), "fixtures")


class _Sink:
    """Minimal runner-shaped sink (the interface parse_trajectory documents)."""
    def __init__(self):
        self.messages = []
        self.handoff = None
        self._agy_seen = set()
        self._agy_noprogress_streak = 0
        self._agy_loop_warned = False
        self._agy_loop_nudge_pending = False
        self.touched = 0
        self.noted = []

    def _touch(self):
        self.touched += 1

    def _note_action(self, name, args):
        self.noted.append((name, args))


def test_parser_reproduces_pre_refactor_stream_exactly():
    expected = json.load(open(os.path.join(FIXDIR, "agy_trajectory_expected.json")))
    sink = _Sink()
    got = AGY.parse_trajectory(os.path.join(FIXDIR, "agy_trajectory.jsonl"), sink)
    assert got == expected["got_answer"]
    assert (sink.handoff or {}).get("reason") == expected["handoff_reason"]
    stream = [{k: v for k, v in m.items() if k != "ts"} for m in sink.messages]
    assert stream == expected["messages"]
    assert sink._agy_noprogress_streak == expected["streak"]
    assert sink.touched > 0                     # steps feed the stall heartbeat
    assert any(n == "browser_navigate" for n, _ in sink.noted)  # unwrapped MCP call


def test_reparse_is_idempotent_via_seen_set():
    """The live poll re-parses the same file every 0.4s — the dedupe set must
    make later passes emit nothing new."""
    sink = _Sink()
    path = os.path.join(FIXDIR, "agy_trajectory.jsonl")
    AGY.parse_trajectory(path, sink)
    n = len(sink.messages)
    AGY.parse_trajectory(path, sink)
    assert len(sink.messages) == n


# ── find_trajectory: strict (live poll) vs lax (final flush) ─────────────────

def _brain(tmp_path, *convs):
    for name, mtime in convs:
        d = tmp_path / name / ".system_generated" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "transcript_full.jsonl"
        p.write_text("{}\n")
        os.utime(p, (mtime, mtime))
    return str(tmp_path)


def test_find_prefers_brand_new_trajectory(tmp_path):
    now = time.time()
    bd = _brain(tmp_path, ("old-conv", now - 100))
    before = AGY.snapshot_trajectories(bd)
    _brain(tmp_path, ("new-conv", now))
    got = AGY.find_trajectory(bd, before)
    assert got and "new-conv" in got


def test_strict_waits_instead_of_locking_onto_a_prior_run(tmp_path):
    """The live poll must NEVER pick a pre-existing trajectory whose mtime
    merely advanced — that was the stale-steps bug (a prior task's thinking
    replayed live). It returns None and waits for the real new file."""
    now = time.time()
    bd = _brain(tmp_path, ("old-conv", now - 100))
    before = AGY.snapshot_trajectories(bd)
    _brain(tmp_path, ("old-conv", now))          # touched, not new
    assert AGY.find_trajectory(bd, before, strict=True) is None
    assert AGY.find_trajectory(bd, before) is not None   # final flush may fall back


def test_strict_resume_accepts_the_existing_trajectory_that_advanced(tmp_path):
    """Agy --conversation appends to the prior brain transcript. Live polling
    must lock onto that touched file when the runner knows this is a resume."""
    now = time.time()
    bd = _brain(tmp_path, ("resumed-conv", now - 100))
    before = AGY.snapshot_trajectories(bd)
    _brain(tmp_path, ("resumed-conv", now))
    got = AGY.find_trajectory(bd, before, strict=True, allow_touched=True)
    assert got and "resumed-conv" in got


def test_resumed_parser_starts_at_the_prelaunch_byte_offset(tmp_path):
    """Re-reading an appended transcript must emit only this turn, not replay
    the previous browser turn into the sandbox trace."""
    path = tmp_path / "transcript_full.jsonl"
    old = {"source": "MODEL", "type": "PLANNER_RESPONSE", "step_index": 3,
           "thinking": "old browser reasoning"}
    path.write_text(json.dumps(old) + "\n")
    offsets = AGY.snapshot_offsets({str(path): path.stat().st_mtime})
    new = {"source": "MODEL", "type": "PLANNER_RESPONSE", "step_index": 5,
           "thinking": "new sandbox reasoning"}
    path.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")
    sink = _Sink()
    sink._agy_offsets = offsets
    AGY.parse_trajectory(str(path), sink)
    texts = [m["text"] for m in sink.messages]
    assert texts == ["new sandbox reasoning"]


def test_find_on_missing_brain_dir_is_none():
    assert AGY.find_trajectory("/nonexistent/brain", {}) is None
    assert AGY.snapshot_trajectories("") == {}


# ── stdout noise filter (user Stop) ──────────────────────────────────────────

def test_stop_noise_lines_are_dropped():
    out = AGY.filter_stop_noise(
        "Error: timed out waiting for response\nreal answer line\n"
        "The operation was canceled")
    assert out == "real answer line"


def test_all_noise_yields_empty(ecmd=None):
    assert AGY.filter_stop_noise("Error: request was aborted") == ""


# ── Flash 3.6 duplicates its final answer (the owner 2026-07-29) ──────────────────
# "the last message of her tool trace is the message she outputs, so there's a
# duplication going on not to mention it spams the thinking trace"
#
# Flash 3.6 rides its narration in `content`. On a step that ALSO carries
# tool_calls that content is CoT → role="thinking" (correct). But it then
# repeats the SAME text on the final, tool_call-less step → role="assistant".
# So the identical paragraph renders twice: once as the last trace step, once
# as the reply bubble.

def _planner(step, content=None, thinking=None, tool_calls=None):
    o = {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE"}
    if content is not None:
        o["content"] = content
    if thinking is not None:
        o["thinking"] = thinking
    if tool_calls is not None:
        o["tool_calls"] = tool_calls
    return o


def _write_traj(tmp_path, steps, name="t.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(s) for s in steps) + "\n")
    return str(p)


ANSWER = "I checked the page and the login form is on the right."


def test_final_answer_is_not_also_left_in_the_thinking_trace(tmp_path):
    """The duplicated paragraph must appear ONCE, as the assistant reply."""
    sink = _Sink()
    traj = _write_traj(tmp_path, [
        _planner(0, content=ANSWER,
                 tool_calls=[{"name": "browser_navigate", "args": {"url": "u"}}]),
        _planner(1, content=ANSWER),          # same text again, no tool_calls
    ])
    assert AGY.parse_trajectory(traj, sink) is True

    texts = [(m["role"], m["text"]) for m in sink.messages]
    assert ("assistant", ANSWER) in texts, f"the reply must survive: {texts}"
    dupes = [t for r, t in texts if r == "thinking" and t == ANSWER]
    assert not dupes, f"answer also left in the thinking trace: {texts}"


def test_genuine_narration_before_a_different_answer_is_kept(tmp_path):
    """Only the DUPLICATE is dropped. Real CoT that differs from the answer is
    the whole value of the trace — it must stay."""
    sink = _Sink()
    traj = _write_traj(tmp_path, [
        _planner(0, content="Let me open the page first.",
                 tool_calls=[{"name": "browser_navigate", "args": {"url": "u"}}]),
        _planner(1, content=ANSWER),
    ], "t2.jsonl")
    assert AGY.parse_trajectory(traj, sink) is True

    texts = [(m["role"], m["text"]) for m in sink.messages]
    assert ("thinking", "Let me open the page first.") in texts, texts
    assert ("assistant", ANSWER) in texts, texts
