"""Metadata-only, bounded instrumentation. Never perform I/O on the frame path."""
from collections import deque
import json
import math
import threading
import time

import operator_workspace as workspace

_LOCK = threading.Lock()
_buckets = {}
_debug = deque(maxlen=1000)
_debug_until = 0.0
_started = False
_last_cleanup = 0.0
_wake = threading.Event()
_METRICS = frozenset(('capture_ms', 'encode_ms', 'frame_bytes', 'frames_sent',
    'frames_unchanged', 'reconnects', 'viewport_corrections', 'tool_ms',
    'first_action_ms', 'run_ms', 'steering_ms', 'interruptions', 'failures',
    'input_tokens', 'output_tokens', 'input_ms', 'capture_encode_ms', 'file_transfer_failures'))


def record(name, value=1, *, run_id='', model=''):
    if name not in _METRICS or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        return
    # Labels contain only stable IDs/model slugs; never accept arbitrary details.
    run_id = ''.join(c for c in str(run_id) if c.isalnum())[:32]
    model = ''.join(c for c in str(model) if c.isalnum() or c in '-_.')[:80]
    key = (int(time.time() // 60), name, run_id, model)
    with _LOCK:
        if key not in _buckets and len(_buckets) >= 2000:
            return
        item = _buckets.setdefault(key, [0, 0.0, 0.0])
        item[0] += 1
        item[1] += value
        item[2] = max(item[2], value)
        if time.time() < _debug_until:
            _debug.append(dict(ts=time.time(), metric=name, value=value, run_id=run_id, model=model))


def flush():
    global _last_cleanup
    with _LOCK:
        batch = dict(_buckets)
        _buckets.clear()
        debug = list(_debug)
        _debug.clear()
    if not batch and not debug and time.time() - _last_cleanup < 300:
        return
    try:
        with workspace.database() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS metrics (
              minute INTEGER, name TEXT, run_id TEXT, model TEXT,
              count INTEGER, total REAL, maximum REAL,
              PRIMARY KEY(minute,name,run_id,model))''')
            db.execute('CREATE TABLE IF NOT EXISTS debug_metrics (ts REAL, event TEXT)')
            for key, vals in batch.items():
                db.execute('''INSERT INTO metrics VALUES (?,?,?,?,?,?,?)
                  ON CONFLICT(minute,name,run_id,model) DO UPDATE SET
                  count=count+excluded.count, total=total+excluded.total,
                  maximum=MAX(maximum,excluded.maximum)''', (*key, *vals))
            db.executemany('INSERT INTO debug_metrics VALUES (?,?)',
                           [(x['ts'], json.dumps(x)) for x in debug])
            db.execute('DELETE FROM metrics WHERE minute<?', (int(time.time() // 60) - 30 * 1440,))
            db.execute('DELETE FROM debug_metrics WHERE ts<?', (time.time() - 86400,))
            db.execute('DELETE FROM debug_metrics WHERE rowid NOT IN (SELECT rowid FROM debug_metrics ORDER BY ts DESC LIMIT 10000)')
            _last_cleanup = time.time()
    except Exception:
        # Measurements are expendable; a busy or full disk must not impede work.
        pass


def start():
    global _started
    with _LOCK:
        if _started:
            return
        _started = True
    def worker():
        while True:
            _wake.wait(10)
            _wake.clear()
            flush()
    threading.Thread(target=worker, name='operator-metrics', daemon=True).start()


def flush_soon():
    _wake.set()


def debug_recording(enabled):
    global _debug_until
    with _LOCK:
        _debug_until = time.time() + 900 if enabled else 0
    return {'debug_until': _debug_until}


def snapshot(run_id=''):
    flush()
    try:
        with workspace.database() as db:
            rows = [dict(row) for row in db.execute('''SELECT name,model,
                SUM(count) AS count,SUM(total) AS total,MAX(maximum) AS maximum
                FROM metrics WHERE minute>? AND (?='' OR run_id=?) GROUP BY name,model''',
                (int(time.time() // 60) - 1440, run_id, run_id))]
    except Exception:
        rows = []
    return {'metrics': rows, 'debug_until': _debug_until, 'window_hours': 24}
