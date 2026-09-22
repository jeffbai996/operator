"""Real MCP process, fixture job only. No provider or browser calls."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import operator_workspace as ws


def test_workspace_tools_in_deployed_fallback_interpreter():
    python = '/usr/bin/python3'
    probe = subprocess.run([python, '-c', 'import numpy,PIL,yaml,playwright'], capture_output=True, timeout=5)
    if probe.returncode:
        if os.environ.get('OPERATOR_REQUIRE_MCP') == '1': pytest.fail('MCP interpreter dependencies missing')
        pytest.skip('host MCP interpreter unavailable')
    run = ws.start_run('a', 'Fixture file')
    folder = ws.root() / 'artifacts' / run['run_id']; folder.mkdir(parents=True)
    (folder / 'fixture.txt').write_text('local fixture')
    env = dict(os.environ, OPERATOR_MCP_PYTHON=python, OPERATOR_RUN_ID=run['run_id'],
               OPERATOR_RUN_CREDENTIAL=run['credential'], OPERATOR_SURFACE='browser')
    calls = [{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
             {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'job_state', 'arguments': {}}},
             {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'job_file',
                  'arguments': {'action': 'publish', 'path': 'fixture.txt'}}}]
    output = subprocess.run(['bash', str(Path(__file__).resolve().parents[1] / 'control/operator-mcp.sh')],
        env=env, input='\n'.join(json.dumps(c) for c in calls) + '\n', capture_output=True, text=True, timeout=10)
    assert output.returncode == 0, output.stderr
    replies = [json.loads(line) for line in output.stdout.splitlines()]
    assert 'job_state' in {t['name'] for t in replies[0]['result']['tools']}
    assert not replies[1]['result'].get('isError') and not replies[2]['result'].get('isError')
    assert ws.snapshot('a')['files'][0]['name'] == 'fixture.txt'

