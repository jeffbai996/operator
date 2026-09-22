import json
import time

import operator_diagnostics as metrics
import operator_workspace as ws


def test_metrics_are_metadata_only_and_aggregated(monkeypatch):
    with metrics._LOCK:
        metrics._buckets.clear(); metrics._debug.clear()
    metrics.debug_recording(True)
    metrics.record('prompt', 'sensitive text')
    metrics.record('page_url', 'https://secret.test/account')
    metrics.record('capture_ms', 4)
    metrics.record('capture_ms', 8)
    metrics.record('capture_ms', float('nan'))
    data = metrics.snapshot()
    assert len(data['metrics']) == 1
    assert data['metrics'][0]['count'] == 2
    assert data['metrics'][0]['total'] == 12
    with ws.database() as db:
        debug = [row[0] for row in db.execute('SELECT event FROM debug_metrics')]
    assert 'sensitive' not in json.dumps(debug)
    assert 'secret.test' not in json.dumps(debug)
    assert metrics._debug_until <= time.time() + 901


def test_hot_path_does_not_touch_database(monkeypatch):
    def forbidden():
        raise AssertionError('I/O on frame path')
    monkeypatch.setattr(ws, 'database', forbidden)
    for _ in range(1000):
        metrics.record('frames_sent')


def test_retention_and_debug_expiry(monkeypatch):
    metrics.record('frames_sent'); metrics.flush()
    with ws.database() as db:
        db.execute('INSERT INTO metrics VALUES (0,?,?,?,?,?,?)', ('frames_sent', '', '', 1, 1, 1))
        db.execute('INSERT INTO debug_metrics VALUES (0,?)', ('expired',))
    metrics._debug_until = time.time() - 1
    metrics.record('frames_sent'); metrics.flush()
    with ws.database() as db:
        assert db.execute('SELECT COUNT(*) FROM metrics WHERE minute=0').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM debug_metrics WHERE ts=0').fetchone()[0] == 0
