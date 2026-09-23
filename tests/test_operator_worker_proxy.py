"""The web process may disappear without owning the worker's active state."""
from __future__ import annotations
import multiprocessing
import os
from pathlib import Path
import socketserver
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, str(Path(__file__).parents[1]))
from flask import Flask, request
from operator_proxy import proxy_request


def _worker(path, ready):
    state = {'turn': 'running'}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (state['turn'] + ':' + self.path).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass
    with socketserver.UnixStreamServer(path, Handler) as server:
        ready.set()
        server.serve_forever()


def _app():
    app = Flask(__name__)
    @app.before_request
    def authorize():
        if request.headers.get('X-Test-Auth') != 'yes': return '', 403
    app.before_request(proxy_request)
    app.add_url_rule('/operator/agent', endpoint='operator.agent', view_func=lambda: 'wrong owner')
    return app


def test_web_recreation_preserves_worker_and_auth(tmp_path, monkeypatch):
    path = str(tmp_path / 'operator.sock')
    monkeypatch.setenv('OPERATOR_WORKER_SOCKET', path)
    monkeypatch.delenv('OPERATOR_WORKER', raising=False)
    context = multiprocessing.get_context('spawn')
    ready = context.Event()
    worker = context.Process(target=_worker, args=(path, ready))
    worker.start()
    try:
        assert ready.wait(5)
        for _ in range(5):
            app = _app()  # independent frontend instances, same live worker
            with app.test_client() as client:
                assert client.get('/operator/agent').status_code == 403
                result = client.get('/operator/agent?since=7', headers={'X-Test-Auth': 'yes'})
                assert result.data == b'running:/operator/agent?since=7'
                assert worker.is_alive()
    finally:
        worker.terminate()
        worker.join(5)


def test_unavailable_worker_does_not_create_a_second_owner(tmp_path, monkeypatch):
    monkeypatch.setenv('OPERATOR_WORKER_SOCKET', str(tmp_path / 'missing.sock'))
    monkeypatch.delenv('OPERATOR_WORKER', raising=False)
    result = _app().test_client().get('/operator/agent', headers={'X-Test-Auth': 'yes'})
    assert result.status_code == 503
    assert result.json['error'] == 'Operator worker unavailable'


def test_worker_unit_drains_its_own_socket_not_the_restarting_frontend(tmp_path):
    from deploy_worker import unit_text
    text = unit_text(tmp_path / 'release', tmp_path / 'python', tmp_path, tmp_path / 'worker.sock')
    assert 'operator_worker.py --drain' in text
    assert 'TimeoutStopSec=infinity' in text
    assert 'KillMode=mixed' in text
    assert 'OPERATOR_WORKER=1' in text
    assert '127.0.0.1:5005/squad/operator/agent' not in text
