"""Stream authenticated Operator requests to its independently owned process."""
from __future__ import annotations
import os
import base64
import json
from urllib.parse import quote
from flask import Response, jsonify, request, stream_with_context
from operator_worker import UnixConnection

_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
        'te', 'trailer', 'transfer-encoding', 'upgrade'}
_TRANSPORT = 'X-Operator-Original-Request'


def owns_endpoint(endpoint):
    return (endpoint or '').startswith(('operator.', 'mcp_internal.operator_'))


def restore_transport():
    """Only installed on the private Unix listener; preserve MCP's original peer.

    The frontend overwrites this header, and never verifies/consumes the MCP
    nonce itself. The worker performs the complete signed capability check.
    """
    if not (request.endpoint or '').startswith('mcp_internal.operator_'):
        return
    try:
        values = json.loads(base64.b64decode(request.headers.get(_TRANSPORT, ''), validate=True))
        if not isinstance(values, list) or len(values) != 3 or not all(isinstance(v, str) for v in values):
            return
        peer, host, path = values
    except (ValueError, TypeError):
        return
    request.environ['werkzeug.proxy_fix.orig'] = {'REMOTE_ADDR': peer, 'HTTP_HOST': host}
    request.environ['squad_store.original_path'] = path


def proxy_request():
    path = os.environ.get('OPERATOR_WORKER_SOCKET')
    if not path or os.environ.get('OPERATOR_WORKER') == '1' or not owns_endpoint(request.endpoint):
        return None
    connection = UnixConnection(path)
    headers = {key: value for key, value in request.headers
               if key.lower() not in _HOP and key.lower() != _TRANSPORT.lower()}
    if (request.endpoint or '').startswith('mcp_internal.operator_'):
        from mcp_internal import _original_values
        headers[_TRANSPORT] = base64.b64encode(json.dumps(_original_values()).encode()).decode()
    headers['Connection'] = 'close'
    target = quote(request.script_root + request.path, safe='/')
    if request.query_string:
        target += '?' + request.query_string.decode('ascii')
    try:
        connection.request(request.method, target, body=request.get_data(), headers=headers)
        upstream = connection.getresponse()
    except (OSError, TimeoutError) as exc:
        connection.close()
        # Never fall back to an in-process runner: that would split ownership.
        return jsonify(ok=False, error='Operator worker unavailable'), 503

    def chunks():
        try:
            while data := upstream.read1(64 * 1024):
                yield data
        finally:
            upstream.close()
            connection.close()

    response = Response(stream_with_context(chunks()), status=upstream.status,
        headers=[(key, value) for key, value in upstream.getheaders() if key.lower() not in _HOP],
        direct_passthrough=True)
    response.call_on_close(connection.close)
    return response
