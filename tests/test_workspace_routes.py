import io
from types import SimpleNamespace

from flask import Blueprint, Flask, jsonify
import pytest

import operator_workspace as ws
import operator_workspace_routes as routes


def client(monkeypatch, demo=False):
    import operator_session
    monkeypatch.setattr(operator_session, 'load', lambda cid: {})
    app = Flask(__name__)
    bp = Blueprint('fixture', __name__)
    def guard(data, cid):
        if data.get('client_id') != 'controller':
            return jsonify(ok=False, error='Take over this chat'), 409
    routes.register(bp, demo=demo, conversation=lambda data: data.get('conversation_id', 'a'),
                    control_guard=guard, runner=lambda: SimpleNamespace(is_running=lambda **kw: False))
    app.register_blueprint(bp)
    return app.test_client()


def test_upload_and_download_have_exact_chat_ownership(monkeypatch):
    web = client(monkeypatch)
    response = web.post('/operator/workspace/files', data={
        'conversation_id': 'a', 'client_id': 'controller', 'file': (io.BytesIO(b'fixture'), 'note.txt')})
    assert response.status_code == 200
    fid = response.json['file']['id']
    assert web.get('/operator/workspace/files/' + fid + '?conversation_id=b').status_code == 404
    downloaded = web.get('/operator/workspace/files/' + fid + '?conversation_id=a')
    assert downloaded.data == b'fixture'
    assert downloaded.headers['X-Content-Type-Options'] == 'nosniff'
    assert 'attachment' in downloaded.headers['Content-Disposition']
    assert web.delete('/operator/workspace/files/' + fid, json={'conversation_id': 'a'}).status_code == 409
    assert web.delete('/operator/workspace/files/' + fid, json={'conversation_id': 'a', 'client_id': 'controller'}).status_code == 200


def test_demo_denies_all_workspace_mutations_and_reads(monkeypatch):
    web = client(monkeypatch, demo=True)
    for path in ('/operator/workspace', '/operator/workspace/files', '/operator/diagnostics'):
        assert web.get(path).status_code == 403
    for path in ('/operator/workspace/job', '/operator/workspace/approval', '/operator/workspace/files', '/operator/diagnostics'):
        assert web.post(path, json={'client_id': 'controller'}).status_code == 403


def test_approval_requires_controller_and_exact_snapshot(monkeypatch):
    web = client(monkeypatch)
    run = ws.start_run('a', 'Book')
    approval = ws.request_approval(run['run_id'], run['credential'], {
        'kind': 'booking', 'destination': 'fixture.test', 'description': 'Two adults, Tuesday at 10'})
    body = dict(conversation_id='a', id=approval['id'], fingerprint=approval['fingerprint'], approved=True)
    assert web.post('/operator/workspace/approval', json=body).status_code == 409
    body['client_id'] = 'controller'
    assert web.post('/operator/workspace/approval', json=dict(body, fingerprint='stale')).status_code == 409
    assert web.post('/operator/workspace/approval', json=body).status_code == 200
    assert web.post('/operator/workspace/approval', json=body).status_code == 409


def test_browser_state_does_not_expose_raw_evidence(monkeypatch):
    web = client(monkeypatch)
    run = ws.start_run('a', 'Task')
    ws.observe(run['run_id'], 'fixture', 'raw tool output', ['https://example.com'])
    data = web.get('/operator/workspace?conversation_id=a').json
    assert 'detail' not in data['job']['evidence'][0]
    assert web.get('/operator/diagnostics?conversation_id=b&run_id=' + run['run_id']).status_code == 404

