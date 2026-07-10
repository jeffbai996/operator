"""1.0.9 T1 — _consume/_consume_codex must survive malformed stream input.

The consume funnel runs on the run thread with no try/except above it: any
exception it leaks kills the reader mid-run and wedges the run (state stays
'running', trace goes silent). Every case here feeds hostile/truncated input
and demands (a) no exception, (b) token state stays sane, (c) well-formed
events still land.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_stream_consume.py -q
"""
import json

import pytest

import operator_agent as OA


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = OA.AgentRunner()
    r._runtime = "claude"
    return r


def _line(obj) -> str:
    return json.dumps(obj) + "\n"


# ── claude stream-json path ──────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "",                          # empty line
    "\n",
    "not json\n",
    '{"type": "assistant"',      # truncated JSON
    "5\n",                       # valid JSON, not a dict
    '"just a string"\n',
    "[1, 2, 3]\n",
    "null\n",
])
def test_garbage_lines_never_raise(runner, raw):
    runner._consume(raw)
    assert runner.messages == []


def test_assistant_with_no_message_is_ignored(runner):
    runner._consume(_line({"type": "assistant"}))
    assert runner.messages == []


def test_content_null_is_ignored(runner):
    runner._consume(_line({"type": "assistant", "message": {"content": None}}))
    assert runner.messages == []


@pytest.mark.parametrize("content", [5, "raw string", {"not": "a list"}])
def test_content_of_wrong_type_is_ignored(runner, content):
    runner._consume(_line({"type": "assistant", "message": {"content": content}}))
    assert runner.messages == []


def test_raw_string_and_int_blocks_are_skipped(runner):
    runner._consume(_line({"type": "assistant", "message": {"content": [
        "bare string block", 42, None,
        {"type": "text", "text": "real text"}]}}))
    assert [m["text"] for m in runner.messages] == ["real text"]


def test_text_block_with_non_string_text_is_skipped(runner):
    runner._consume(_line({"type": "assistant", "message": {"content": [
        {"type": "text", "text": 12345}]}}))
    assert runner.messages == []


def test_tool_use_with_non_string_name_is_safe(runner):
    runner._consume(_line({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": 42, "input": {"x": 1}}]}}))
    # no crash; nothing meaningful to label


def test_result_of_wrong_type_is_ignored(runner):
    runner._consume(_line({"type": "result", "result": 42}))
    runner._consume(_line({"type": "result", "result": {"nested": "dict"}}))
    assert runner.messages == []


def test_usage_as_non_dict_keeps_token_state_sane(runner):
    runner._consume(_line({"type": "assistant",
                           "message": {"usage": "oops", "content": []}}))
    assert runner._peak_in_tokens == 0
    assert runner._cum_in_tokens == 0


def test_wellformed_usage_lands_input_tokens(runner):
    runner._consume(_line({"type": "assistant", "message": {
        "usage": {"input_tokens": 1234},
        "content": [{"type": "text", "text": "hi"}]}}))
    assert runner._peak_in_tokens == 1234
    assert runner._cum_in_tokens == 1234
    assert runner.messages[-1]["text"] == "hi"


def test_usage_with_garbage_input_tokens_is_ignored(runner):
    runner._consume(_line({"type": "assistant", "message": {
        "usage": {"input_tokens": "many"}, "content": []}}))
    assert runner._peak_in_tokens == 0


def test_init_event_with_non_string_session_id_is_not_stored(runner):
    runner._consume(_line({"type": "system", "subtype": "init", "session_id": 99}))
    assert runner._cur_session == ""     # a non-str id would corrupt --resume argv


# ── codex JSONL path ─────────────────────────────────────────────────────────

@pytest.fixture
def codex(runner):
    runner._runtime = "codex"
    return runner


@pytest.mark.parametrize("evt", [
    {},                                          # no type
    {"type": "item.completed"},                  # no item
    {"type": "item.completed", "item": 5},       # item not a dict
    {"type": "item.completed", "item": {"type": "agent_message", "text": 42}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": 42}},
    {"type": "token_count"},                     # no usage info at all
    {"type": "token_count", "info": "oops"},     # info not a dict
    {"type": "token_count", "usage": "oops"},
    {"type": "token_count", "info": {"last_token_usage": "oops"}},
    {"type": "error", "message": 42},
])
def test_codex_malformed_events_never_raise(codex, evt):
    codex._consume(_line(evt))


def test_codex_thread_id_of_wrong_type_is_not_stored(codex):
    codex._consume(_line({"type": "thread.started", "thread_id": 123}))
    assert codex._cur_session == ""


def test_codex_wellformed_message_still_lands(codex):
    codex._consume(_line({"type": "item.completed", "item": {
        "type": "agent_message", "text": "codex says hi"}}))
    assert codex.messages[-1]["text"] == "codex says hi"


def test_codex_tool_call_with_garbage_arguments_is_safe(codex):
    codex._consume(_line({"type": "item.completed", "item": {
        "type": "mcp_tool_call", "tool": 42, "arguments": "{broken json"}}))
    # no crash; the trace simply gets no label for it


def test_codex_wellformed_token_count_lands(codex):
    codex._consume(_line({"type": "token_count",
                          "info": {"last_token_usage": {"input_tokens": 777}}}))
    assert codex._peak_in_tokens == 777


# ── the funnel keeps the run's heartbeat even on garbage ─────────────────────

def test_any_line_still_touches_progress(runner):
    t0 = runner.last_progress_ts
    runner._consume("complete garbage\n")
    assert runner.last_progress_ts > t0   # stall watchdog must not starve
