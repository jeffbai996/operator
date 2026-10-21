from types import SimpleNamespace

import pytest
import operator_agent as agent
import operator_steer as queue
import operator_workspace as ws


def runner(monkeypatch, tmp_path):
    monkeypatch.setenv('OPERATOR_STATE_PATH', str(tmp_path / 'state.json'))
    monkeypatch.setenv('OPERATOR_STEER_PATH', str(tmp_path / 'steer.ndjson'))
    r = agent.AgentRunner('a')
    r._workspace_run = ws.start_run('a', 'Fixture')
    r._runtime = 'claude'
    monkeypatch.setattr(r, 'is_running', lambda: True)
    rid = r._workspace_run['run_id']
    ws.correction('a', rid, 'correction', 'Tuesday')
    return r, rid


def test_hook_delivery_does_not_interrupt(monkeypatch, tmp_path):
    r, rid = runner(monkeypatch, tmp_path)
    def boundary(_):
        items = queue.take_all('a')
        for item in items: ws.correction_status(item['id'], 'delivered')
    monkeypatch.setattr(agent.time, 'sleep', boundary)
    r._deliver_correction('Tuesday', 'correction', rid, r._steer_generation)
    assert not r._redirect_prompt
    assert ws.snapshot('a')['corrections'][0]['status'] == 'delivered'


def test_fallback_queues_replacement_without_spawning_it(monkeypatch, tmp_path):
    r, rid = runner(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    r._proc = None  # startup gap; the run thread, not this worker, owns Popen
    monkeypatch.setattr(agent.subprocess, 'Popen', lambda *a, **kw: pytest.fail('replacement started before old run exits'))
    r._deliver_correction('Tuesday', 'correction', rid, r._steer_generation)
    assert 'Tuesday' in r._redirect_prompt and 'inspect its outcome' in r._redirect_prompt
    assert r._steer_delivery_ids == ['correction']


def test_stop_invalidates_delivery_generation(monkeypatch, tmp_path):
    r, rid = runner(monkeypatch, tmp_path)
    def stopped(_):
        r._steer_generation += 1
        r._cancel_requested = True
        ws.finish_run(rid, 'interrupted')
    monkeypatch.setattr(agent.time, 'sleep', stopped)
    r._deliver_correction('Tuesday', 'correction', rid, r._steer_generation)
    assert not r._redirect_prompt
    assert ws.snapshot('a')['corrections'][0]['status'] == 'failed'


def test_native_lost_ack_never_redispatches(monkeypatch, tmp_path):
    r, rid = runner(monkeypatch, tmp_path)
    r._runtime = 'codex'
    monkeypatch.setenv('OPERATOR_CODEX_APP_SERVER', '1')
    def lost(_): raise TimeoutError('lost acknowledgement')
    r._codex_transport = SimpleNamespace(turn_id='turn', steer=lost)
    r._deliver_correction('Tuesday', 'correction', rid, r._steer_generation)
    assert not queue.pending('a') and not r._redirect_prompt
    assert ws.snapshot('a')['corrections'][0]['status'] == 'failed'


def test_a_steer_keeps_what_the_interrupted_run_had_done(monkeypatch, tmp_path):
    """the owner 2026-09-16, steering mid-booking: "it completely lost the thread
    outta nowhere" — the agent went back to re-searching hotels it had already
    chosen between.

    The fallback path kills the run and drops the resume id, so the next turn
    is a FRESH session that gets briefed from `_transcript`. But assistant
    text only reached `_transcript` on a CLEAN finish, and an interrupted run
    never gets there — so the replacement inherited the original task and the
    steer, and nothing about the half-done work in between.
    """
    r, rid = runner(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    r._proc = None
    r._transcript = [{"role": "user", "text": "book us a hotel in Whistler"}]
    r.messages = [
        {"ts": 1, "role": "assistant", "text": "Comparing Westin and Four Seasons."},
        {"ts": 2, "role": "assistant", "text": "Opening the Westin booking form at the 297 rate."},
    ]

    r._deliver_correction('quiet room', 'correction', rid, r._steer_generation)

    carried = [m for m in r._transcript if m["role"] == "assistant"]
    assert carried, "the interrupted run's progress never reached the transcript"
    assert "Westin booking form" in carried[-1]["text"]
    # and the replacement is told the work is mid-flight, not starting over
    assert 'quiet room' in r._redirect_prompt


def test_nothing_is_invented_when_the_run_had_said_nothing(monkeypatch, tmp_path):
    r, rid = runner(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    r._proc = None
    r._transcript = [{"role": "user", "text": "book us a hotel"}]
    r.messages = [{"ts": 1, "role": "error", "text": "some failure"}]

    r._deliver_correction('quiet room', 'correction', rid, r._steer_generation)

    assert [m["role"] for m in r._transcript] == ["user"]


def _redirected_runner(monkeypatch, tmp_path, bot):
    monkeypatch.setenv('OPERATOR_STATE_PATH', str(tmp_path / 'state.json'))
    monkeypatch.setenv('OPERATOR_SESSION_ROOT', str(tmp_path / 'sessions'))
    monkeypatch.setattr(agent, '_squad_boot_context', lambda *a: '')
    r = agent.AgentRunner('a')
    r.state = 'running'
    r.bot = bot
    r.started_ts = 1
    r._save_state = lambda: None
    r._completion_gate_check = lambda: None
    r._steer_followup_check = lambda: None
    r._persona_for_run = lambda b: ''
    r._reap_owned_browser_helpers = lambda: 0
    monkeypatch.setattr(agent.operator_runtimes, 'cleanup_launch_plan', lambda p: None)
    return r


def test_a_correction_resumes_the_claude_session(monkeypatch, tmp_path):
    """The replacement turn after a correction started a FRESH session briefed
    from final text alone, so it forgot its own clicks: it had put a Venti in
    the cart, then "fixed" a Tall (the owner 2026-10-19, "retrograde amnesia"). A
    killed claude session resumes with its tool history, so its id is kept."""
    import io
    import json
    import subprocess
    from types import SimpleNamespace
    r = _redirected_runner(monkeypatch, tmp_path, 'claude-a')

    def stdout():
        yield json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'claude-thread-1'}) + '\n'
        r._redirect_prompt = 'Actually make that a trenta'   # the correction worker killed the run

    proc = SimpleNamespace(pid=1234567, stdin=io.StringIO(), stdout=stdout(), returncode=None)
    proc.poll = lambda: proc.returncode

    def wait(timeout=None):
        proc.returncode = -15
        return -15
    proc.wait = wait
    monkeypatch.setattr(agent.subprocess, 'Popen', lambda *a, **k: proc)
    monkeypatch.setattr(agent.operator_runtimes, 'build_cmd',
                        lambda *a: SimpleNamespace(cmd=['claude', '-p', 'hello'], env={}))

    followup = r._run_inner('claude', {'runtime': 'claude', 'config_dir': ''}, 'order a cold brew')

    assert followup == 'Actually make that a trenta'
    assert r._session_ids['claude-a'] == 'claude-thread-1'


def test_a_correction_still_drops_a_killed_codex_thread(monkeypatch, tmp_path):
    """codex keeps the old rule: a half-killed native thread is not resumed."""
    import io
    from types import SimpleNamespace
    import operator_codex_transport as transport
    monkeypatch.setenv('OPERATOR_CODEX_APP_SERVER', '1')
    r = _redirected_runner(monkeypatch, tmp_path, 'gpt')
    proc = SimpleNamespace(pid=1234567, stdin=io.StringIO(), returncode=None)
    proc.poll = lambda: proc.returncode

    def wait(timeout=None):
        proc.returncode = -15
        return -15
    proc.wait = wait
    monkeypatch.setattr(transport.os, 'killpg', lambda pid, sig: None)
    monkeypatch.setattr(agent.subprocess, 'Popen', lambda *a, **k: proc)
    monkeypatch.setattr(agent.operator_runtimes, 'build_cmd',
                        lambda *a: SimpleNamespace(cmd=['codex', 'exec', 'hello'], env={}))

    class Wire:
        thread_id = 'codex-thread-1'

        def __init__(self, process): pass

        def begin(self, **kwargs): pass

        def notifications(self):
            r._redirect_prompt = 'Actually make that a trenta'
            yield {'method': 'turn/completed', 'params': {'turn': {'status': 'interrupted'}}}
    monkeypatch.setattr(transport, 'AppServer', Wire)

    followup = r._run_inner('codex', {'runtime': 'codex', 'config_dir': ''}, 'order a cold brew')

    assert followup == 'Actually make that a trenta'
    assert 'gpt' not in r._session_ids


def test_the_next_prompt_carries_what_the_user_did_by_hand(monkeypatch, tmp_path):
    """A manual-control note (operator_steer.note_manual) rides at the top of
    whatever prompt _run_inner builds next, then is consumed."""
    import io
    from types import SimpleNamespace
    monkeypatch.setenv('OPERATOR_STEER_PATH', str(tmp_path / 'steer.ndjson'))
    r = _redirected_runner(monkeypatch, tmp_path, 'claude-a')
    queue.note_manual({'kind': 'click_at'}, 'a', url='https://shop.test/cart')
    seen = []
    proc = SimpleNamespace(pid=1234567, stdin=io.StringIO(), stdout=iter(()), returncode=None)
    proc.poll = lambda: proc.returncode

    def wait(timeout=None):
        proc.returncode = 0
        return 0
    proc.wait = wait
    monkeypatch.setattr(agent.subprocess, 'Popen', lambda *a, **k: proc)

    def build(runtime, spec):
        seen.append(spec.task)
        return SimpleNamespace(cmd=['claude', '-p', spec.task], env={})
    monkeypatch.setattr(agent.operator_runtimes, 'build_cmd', build)

    r._run_inner('claude', {'runtime': 'claude', 'config_dir': ''}, 'now add a sandwich')

    assert len(seen) == 1
    note = seen[0].index('[The user drove the browser themselves')
    assert 'https://shop.test/cart' in seen[0]
    assert note < seen[0].index('now add a sandwich')   # the note precedes the request
    assert queue.pending_manual('a') is None
