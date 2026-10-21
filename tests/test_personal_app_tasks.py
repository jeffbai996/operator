"""Personal app tasks reuse the runner without pretending every task is browsing."""
import pytest
import operator_agent as OA
import operator_prompts as prompts
import operator_session as sessions

@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    return OA.AgentRunner(conversation_id='owned-app')

def test_document_task_keeps_its_verified_file_answer(runner, monkeypatch):
    monkeypatch.setattr(sessions, 'is_personal_app', lambda cid: cid == 'owned-app')
    runner.surface = 'browser'
    runner._configure_task_purpose('Create a checklist from the uploaded note')
    assert runner._personal_app and not runner._browser_required
    runner._note_action('write_file', {'path': '/tmp/checklist.txt'})
    runner.messages.append({'ts': 0, 'role': 'assistant', 'text': 'Checklist attached.'})
    assert runner._completion_gate_check() == ''
    assert not runner._browser_contract_failed
    assert runner.messages[-1]['text'] == 'Checklist attached.'

@pytest.mark.parametrize('demo,owned', [(False, False), (True, True)])
def test_cockpit_and_demo_keep_browser_contract(runner, monkeypatch, demo, owned):
    monkeypatch.setattr(sessions, 'is_personal_app', lambda cid: owned)
    runner.surface = 'browser'
    runner._configure_task_purpose('Find live ticket prices', demo)
    assert runner._browser_required and not runner._personal_app

@pytest.mark.parametrize('model', ['', 'gpt-6-astra'])
def test_persona_keeps_tool_boundaries_and_adds_general_task_context(model):
    persona = prompts.build_persona('Existing identity.' + prompts.BROWSER_MANDATE,
                                    'browser', False, model=model, personal_app=True)
    assert "Existing identity." not in persona
    assert "You are Operator" not in persona
    assert prompts.PERSONAL_APP_DIRECTIVE in persona
    assert prompts.ONEPASS_RECIPIENT_RULE in persona
    assert prompts.STORED_DATA_RECIPIENT_RULE in persona
    assert prompts.BROWSER_MANDATE not in persona


def test_client_chat_data_cannot_change_task_purpose(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, '_PATH', tmp_path / 'sessions.json')
    # Use the module's normal environment mechanism rather than private state.
    monkeypatch.setenv('OPERATOR_SESSION_PATH', str(tmp_path / 'sessions.json'))
    import importlib
    importlib.reload(sessions)
    try:
        normal = sessions.create('Normal', reuse_draft=False)
        sessions.save({'origin': 'app'}, normal['id'])
        app = sessions.create('App', activate=False, reuse_draft=False, origin='app')
        assert not sessions.is_personal_app(normal['id'])
        assert sessions.is_personal_app(app['id'])
        assert not sessions.is_personal_app('missing')
    finally:
        monkeypatch.undo()
        importlib.reload(sessions)
