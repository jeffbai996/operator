"""CDP liveness requires browser identity and a usable browser websocket."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = next((p for p in (_ROOT / 'cdp_metadata.py', _ROOT / 'browse/cdp_metadata.py')
                if p.exists()), _ROOT / 'cdp_metadata.py')


@pytest.mark.parametrize('payload,expected', [
    ({'Browser': 'Chrome/140.0', 'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/browser/example'}, 0),
    ({'Browser': 'HeadlessChrome/140.0', 'webSocketDebuggerUrl': 'ws://localhost:9222/devtools/browser/example'}, 0),
    ({'Browser': 'Chromium/140.0', 'webSocketDebuggerUrl': 'ws://[::1]:9222/devtools/browser/example'}, 0),
    ({'Browser': 'OtherService/1', 'webSocketDebuggerUrl': 'ws://localhost/devtools/browser/example'}, 1),
    ({'Browser': 'Chrome/140.0'}, 1),
    ({'Browser': 'Chrome/140.0', 'webSocketDebuggerUrl': 'http://localhost/devtools/browser/example'}, 1),
    ({'Browser': 'Chrome/140.0', 'webSocketDebuggerUrl': 'ws:///devtools/browser/example'}, 1),
    ({'Browser': 'Chrome/140.0', 'webSocketDebuggerUrl': 'ws://localhost/devtools/page/example'}, 1),
    ({'Browser': 'Chrome/140.0', 'webSocketDebuggerUrl': 'ws://localhost:invalid/devtools/browser/example'}, 1),
    ({}, 1),
])
def test_http_200_is_not_sufficient_for_cdp_liveness(payload, expected):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([sys.executable, str(_SCRIPT), '--url',
                                 f'http://127.0.0.1:{server.server_port}/json/version'])
        assert result.returncode == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_malformed_stdin_is_not_a_live_browser():
    result = subprocess.run([sys.executable, str(_SCRIPT)], input='not json', text=True)
    assert result.returncode == 1
