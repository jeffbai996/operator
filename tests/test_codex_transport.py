"""Recorded protocol fixtures only. Never contacts Codex or a model."""
import ast
import json
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from operator_codex_transport import AppServer, TransportError, cli_event, command


class Wire:
    def __init__(self):
        self.output = queue.Queue()
        self.sent = []
        self.replies = {'initialize': {}, 'thread/start': {'thread': {'id': 'thread-1'}},
                        'thread/resume': {'thread': {'id': 'thread-1'}},
                        'turn/start': {'turn': {'id': 'turn-1'}}, 'turn/steer': {'turnId': 'turn-1'}}
        self.process = SimpleNamespace(stdin=self, stdout=iter(self.output.get, None))

    def write(self, line):
        msg = json.loads(line)
        self.sent.append(msg)
        if 'id' in msg and msg.get('method') in self.replies:
            self.emit({'id': msg['id'], 'result': self.replies[msg['method']]})

    def flush(self):
        pass

    def emit(self, event):
        self.output.put(json.dumps(event) + '\n')


@pytest.fixture
def pair():
    wire = Wire()
    transport = AppServer(wire.process)
    yield wire, transport
    wire.output.put(None)
    transport._reader.join(timeout=1)


def begin(transport, **kwargs):
    transport.begin(prompt='fixture task', cwd='/fixture', model='fixture-model', effort='high', **kwargs)


def test_client_version_matches_operator_release(pair):
    # Read the literal without importing Flask/runner/browser machinery into
    # this isolated wire fixture. A bump must cover both advertised versions.
    source = Path(__file__).resolve().parents[1] / 'operator_view.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    version = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign) and any(
                       isinstance(target, ast.Name) and target.id == 'OP_VERSION'
                       for target in node.targets))
    wire, transport = pair
    begin(transport)
    assert wire.sent[0]['params']['clientInfo']['version'] == version


def test_native_start_and_steer_are_same_owned_turn(pair):
    wire, transport = pair
    begin(transport)
    transport.steer('make it smaller')
    methods = [m.get('method') for m in wire.sent]
    assert methods == ['initialize', 'initialized', 'thread/start', 'turn/start', 'turn/steer']
    assert wire.sent[-1]['params']['expectedTurnId'] == 'turn-1'
    assert wire.sent[-1]['params']['input'][0]['text'] == 'make it smaller'


def test_resume_does_not_create_another_thread(pair):
    wire, transport = pair
    begin(transport, resume_id='thread-1')
    assert wire.sent[2]['method'] == 'thread/resume'
    assert wire.sent[2]['params']['threadId'] == 'thread-1'


def test_dispatch_timeout_is_not_replayed(pair):
    wire, transport = pair
    del wire.replies['turn/start']
    original = transport.request
    transport.request = lambda method, params, timeout=10: original(method, params, timeout=.01)
    with pytest.raises(TransportError, match='timed out'):
        begin(transport)
    assert transport.dispatched
    assert sum(m.get('method') == 'turn/start' for m in wire.sent) == 1


def test_foreign_turn_events_cannot_complete_this_run(pair):
    wire, transport = pair
    begin(transport)
    wire.emit({'method': 'turn/plan/updated', 'params': {'threadId': 'foreign', 'plan': []}})
    wire.emit({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1', 'status': 'completed'}}})
    assert len(list(transport.notifications())) == 1


def test_unknown_server_requests_are_never_autoapproved(pair):
    wire, transport = pair
    wire.emit({'id': 800, 'method': 'item/commandExecution/requestApproval', 'params': {}})
    wire.output.put(None)
    transport._reader.join(timeout=1)
    assert wire.sent[-1]['error']['code'] == -32601


def test_command_preserves_config_and_item_projection():
    plan = SimpleNamespace(cmd=['codex', 'exec', '--json', '--skip-git-repo-check',
        '-c', 'mcp_servers.playwright.env.BROWSE_CHROME_PORT="9222"', '-m', 'fixture',
        'resume', 'thread-1', 'private task'])
    assert command(plan) == ['codex', 'app-server', '-c', 'mcp_servers.playwright.env.BROWSE_CHROME_PORT="9222"']
    event = cli_event({'method': 'item/completed', 'params': {'item': {
        'id': 'a', 'type': 'mcpToolCall', 'tool': 'browser_snapshot', 'result': {'content': []}}}})
    assert event['item']['type'] == 'mcp_tool_call'
    assert event['item']['tool'] == 'browser_snapshot'


def test_requests_match_installed_cli_schema_without_model_calls(pair, tmp_path):
    import shutil
    import subprocess
    from pathlib import Path
    jsonschema = pytest.importorskip('jsonschema')
    binary = shutil.which('codex')
    if not binary:
        pytest.skip('installed Codex CLI schema unavailable')
    subprocess.run([binary, 'app-server', 'generate-json-schema', '--out', str(tmp_path)],
                   check=True, capture_output=True, timeout=15)
    wire, transport = pair
    begin(transport)
    transport.steer('fixture correction')
    names = {'initialize': 'v1/InitializeParams', 'thread/start': 'v2/ThreadStartParams',
             'turn/start': 'v2/TurnStartParams', 'turn/steer': 'v2/TurnSteerParams'}
    for message in wire.sent:
        name = names.get(message.get('method'))
        if name:
            schema = json.loads((Path(tmp_path) / (name + '.json')).read_text())
            jsonschema.validate(message['params'], schema)
