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
