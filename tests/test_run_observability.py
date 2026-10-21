"""Owned-process cleanup and public run telemetry. No live model calls."""
import io
import json
import signal
import subprocess
from types import SimpleNamespace

import operator_agent as OA
import operator_codex_transport as transport
from operator_observability import public_data, run_error


def test_completed_turn_survives_two_shutdown_timeouts(monkeypatch, tmp_path):
    monkeypatch.setenv('OPERATOR_CODEX_APP_SERVER', '1')
    monkeypatch.setenv('OPERATOR_STATE_PATH', str(tmp_path / 'state.json'))
    monkeypatch.setattr(OA, '_squad_boot_context', lambda *a: '')
    runner = OA.AgentRunner()
    runner.state = 'running'
    runner.bot = 'gpt'
    runner.started_ts = 1
    runner._personal_app = True
    runner._save_state = lambda: None
    runner._completion_gate_check = lambda: None
    runner._steer_followup_check = lambda: None
    runner._persona_for_run = lambda b: ''
    signals, waits = [], []
    proc = SimpleNamespace(pid=1234567, stdin=io.StringIO(), returncode=None)
    proc.poll = lambda: proc.returncode
    def wait(timeout):
        waits.append(timeout)
        if len(waits) < 3:
            raise subprocess.TimeoutExpired(['codex', 'OPERATOR_RUN_CREDENTIAL=fixture-private'], timeout)
        proc.returncode = -9
        return -9
    proc.wait = wait
    monkeypatch.setattr(transport.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(OA.subprocess, 'Popen', lambda *a, **k: proc)
    monkeypatch.setattr(OA.operator_runtimes, 'build_cmd', lambda *a: SimpleNamespace(cmd=['codex', 'exec', 'hello'], env={}))
    monkeypatch.setattr(OA.operator_runtimes, 'cleanup_launch_plan', lambda p: None)
    class Wire:
        thread_id = 'fixture-thread'
        def __init__(self, process): pass
        def begin(self, **kwargs): pass
        def notifications(self):
            yield {'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': 'Saved answer'}}}
            yield {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}
    monkeypatch.setattr(transport, 'AppServer', Wire)
    runner._run_inner('codex', {'runtime': 'codex', 'config_dir': ''}, 'hello')
    assert runner.state == 'done'
    assert runner._proc is None
    assert waits == [2, 2, 2]
    assert signals == [(1234567, signal.SIGTERM), (1234567, signal.SIGKILL)]
    assert runner._session_ids['gpt'] == 'fixture-thread'
    assert runner.messages[-1]['text'] == 'Saved answer'
    assert 'fixture-private' not in json.dumps(runner.snapshot())


def test_exception_and_legacy_command_are_safe():
    error = subprocess.TimeoutExpired(['codex', 'app-server', '-c', 'OPERATOR_RUN_CREDENTIAL=fixture-private'], 2)
    for text in [run_error(error), public_data({'text': str(error)})['text']]:
        assert 'fixture-private' not in text
        assert 'OPERATOR_RUN_CREDENTIAL' not in text
    assert 'fixture-private' not in run_error(RuntimeError('OPERATOR_RUN_CREDENTIAL="fixture-private"'))


def test_native_summary_stream_is_one_public_summary(monkeypatch, tmp_path):
    monkeypatch.setenv('OPERATOR_STATE_PATH', str(tmp_path / 'state.json'))
    runner = OA.AgentRunner()
    for delta in ['Checking ', 'the options']:
        runner._consume_native_codex({'method': 'item/reasoning/summaryTextDelta', 'params': {'itemId': 'r1', 'delta': delta}})
    assert runner.messages[0]['text'] == 'Checking the options'
    runner._consume_native_codex({'method': 'item/completed', 'params': {'item': {
        'id': 'r1', 'type': 'reasoning', 'summary': ['Checking the options'], 'content': ['private raw reasoning']}}})
    assert len(runner.messages) == 1
    assert 'private raw' not in json.dumps(runner.snapshot())


def test_native_usage_is_per_run_and_duplicate_safe(monkeypatch, tmp_path):
    monkeypatch.setenv('OPERATOR_STATE_PATH', str(tmp_path / 'state.json'))
    runner = OA.AgentRunner()
    def event(total, last):
        return {'method': 'thread/tokenUsage/updated', 'params': {'tokenUsage': {'total': total, 'last': last}}}
    last = dict(inputTokens=100, outputTokens=20, cachedInputTokens=40, reasoningOutputTokens=5)
    # Resumed thread already contains prior turns: count only this run.
    total = dict(inputTokens=1100, outputTokens=220, cachedInputTokens=440, reasoningOutputTokens=55)
    runner._consume_native_codex(event(total, last))
    runner._consume_native_codex(event(total, last))
    assert runner._native_usage == last
    next_total = {k: v + last[k] for k, v in total.items()}
    runner._consume_native_codex(event(next_total, last))
    assert runner._cumulative_in_tokens == 200
    assert runner._cumulative_out_tokens == 40
    assert runner.snapshot()['usage']['reasoningOutputTokens'] == 10


def test_native_tool_start_and_completion_share_activity(monkeypatch, tmp_path):
    monkeypatch.setenv('OPERATOR_STATE_PATH', str(tmp_path / 'state.json'))
    runner = OA.AgentRunner()
    runner._runtime = 'codex'
    for phase in ['started', 'completed']:
        runner._consume_native_codex({'method': 'item/' + phase, 'params': {'item': {
            'id': 'tool1', 'type': 'mcpToolCall', 'tool': 'browser_snapshot', 'arguments': {}, 'status': phase}}})
        assert len(runner.messages) == 1
    assert runner.messages[0]['status'] == 'completed'
    assert not runner._tool_active


def _finish_native_tool(monkeypatch, tmp_path, item_type, **fields):
    monkeypatch.setenv('OPERATOR_STATE_PATH', str(tmp_path / 'state.json'))
    runner = OA.AgentRunner()
    runner._runtime = 'codex'
    for phase in ['started', 'completed']:
        item = {'id': 'tool1', 'type': item_type, 'tool': 'browser_click',
                'command': 'rm -rf build', 'arguments': {}}
        if phase == 'completed':
            item.update(fields)
        runner._consume_native_codex({'method': 'item/' + phase, 'params': {'item': item}})
    assert len(runner.messages) == 1
    return runner.messages[0]


def test_declined_native_command_is_not_recorded_as_completed(monkeypatch, tmp_path):
    message = _finish_native_tool(monkeypatch, tmp_path, 'commandExecution', status='declined')
    assert message['status'] == 'declined'


def test_unrecognised_native_tool_status_is_not_recorded_as_completed(monkeypatch, tmp_path):
    message = _finish_native_tool(monkeypatch, tmp_path, 'mcpToolCall', status='somethingNew')
    assert message['status'] == 'unknown'


def test_failed_native_tool_status_stays_failed(monkeypatch, tmp_path):
    message = _finish_native_tool(monkeypatch, tmp_path, 'mcpToolCall', status='failed')
    assert message['status'] == 'failed'


class TargetClosedError(Exception):
    """Stands in for playwright._impl._errors.TargetClosedError."""


def _loop_with_default_spy(monkeypatch):
    import asyncio
    loop = asyncio.new_event_loop()
    seen = []
    monkeypatch.setattr(loop, 'default_exception_handler', seen.append)
    return loop, seen


def test_orphaned_closed_target_futures_are_not_logged_as_errors(monkeypatch):
    from operator_observability import quiet_closed_target_futures
    loop, seen = _loop_with_default_spy(monkeypatch)
    try:
        quiet_closed_target_futures(loop, {
            'message': 'Future exception was never retrieved',
            'exception': TargetClosedError('Target page, context or browser has been closed')})
    finally:
        loop.close()
    assert seen == []


def test_other_loop_errors_still_reach_the_default_handler(monkeypatch):
    from operator_observability import quiet_closed_target_futures
    loop, seen = _loop_with_default_spy(monkeypatch)
    contexts = [
        {'message': 'Future exception was never retrieved', 'exception': ValueError('real bug')},
        {'message': 'Task exception was never retrieved',
         'exception': TargetClosedError('closed')},
    ]
    try:
        for context in contexts:
            quiet_closed_target_futures(loop, context)
    finally:
        loop.close()
    assert seen == contexts
