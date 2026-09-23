"""Own Operator runners independently of the host-app web process.

Only a private Unix socket is exposed. The public process still applies its
route policy before proxying, and the worker applies the same policy again.
"""
from __future__ import annotations
import http.client
import os
from pathlib import Path
import socket
import sys


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 120):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def socket_path() -> str:
    return os.environ.get('OPERATOR_WORKER_SOCKET',
        str(Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / f'operator-{os.getuid()}.sock'))


def main() -> None:
    os.umask(0o077)
    if '--drain' in sys.argv:
        from operator_restart_guard import wait_until_idle
        def opener(_url, timeout=3):
            connection = UnixConnection(socket_path(), timeout=timeout)
            connection.request('GET', '/squad/operator/agent?conversation_id=worker-drain',
                               headers={'Host': '127.0.0.1:5005', 'Connection': 'close'})
            return connection.getresponse()
        wait_until_idle('worker', opener=opener)
        return
    os.environ['OPERATOR_WORKER'] = '1'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host-app'))
    import server
    from flask import abort, request
    from werkzeug.serving import run_simple
    errors = server.production_security_errors()
    if errors:
        raise SystemExit('; '.join(errors))

    @server.app.before_request
    def operator_only():
        from operator_proxy import owns_endpoint, restore_transport
        if not owns_endpoint(request.endpoint):
            abort(404)
        restore_transport()

    # The main server no longer owns tab cleanup when the worker is enabled.
    import operator_tab_cleanup
    import operator_view
    operator_tab_cleanup.start(operator_view)
    run_simple('unix://' + socket_path(), 0, server.app, threaded=True,
               use_reloader=False, use_debugger=False)


if __name__ == '__main__':
    main()
