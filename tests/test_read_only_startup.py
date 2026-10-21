"""Proxy/replica imports must not recover runs or start duplicate housekeeping."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('read_only,demo,expected', [
    (None, '0', ['recover', 'metrics', 'schedule']),
    ('0', '0', ['recover', 'metrics', 'schedule']),
    ('1', '0', []),
    (' 1 ', '0', []),
    (None, '1', []),
])
@pytest.mark.parametrize('worker,socket', [
    (None, None),
    ('1', '/tmp/operator-test.sock'),
    (None, '/tmp/operator-test.sock'),
])
def test_import_housekeeping_only_runs_on_writable_private_instance(
        read_only, demo, expected, worker, socket):
    env = dict(os.environ, OPERATOR_TESTING='0', OPERATOR_DEMO=demo)
    for key, value in [('OPERATOR_WORKER', worker), ('OPERATOR_WORKER_SOCKET', socket)]:
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    if socket and worker != '1':
        expected = []  # the proxy must never invalidate its worker's live runs
    if read_only is None:
        env.pop('SQUAD_STORE_READ_ONLY', None)
    else:
        env['SQUAD_STORE_READ_ONLY'] = read_only
    probe = '''
import json
import operator_schedule
import operator_workspace
import operator_diagnostics
events = []
operator_workspace.recover_runs = lambda: events.append('recover')
operator_diagnostics.start = lambda: events.append('metrics')
operator_schedule.start = lambda **kw: events.append('schedule')
import operator_view
print(json.dumps(events))
'''
    result = subprocess.run([sys.executable, '-c', probe], env=env,
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == expected
