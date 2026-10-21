"""Personal-app controls over the existing conversation workspace.

The gateway enforces account ownership; this module keeps resources bound to
their upstream conversation and uses revision checks for user edits.
"""
import json
import time

import operator_workspace as workspace


def _text(value, limit=2000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('invalid text')
    return value.strip()


def _setup(db):
    db.execute('''CREATE TABLE IF NOT EXISTS app_objects (
        id TEXT NOT NULL, conversation_id TEXT NOT NULL, kind TEXT NOT NULL,
        revision INTEGER NOT NULL, updated REAL NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(id, revision))''')


def goal(cid, data):
    with workspace.database() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM deleted_conversations WHERE id=?', (cid,)).fetchone():
            raise workspace.Conflict('conversation was deleted')
        row = db.execute('SELECT * FROM jobs WHERE conversation_id=? ORDER BY updated DESC LIMIT 1', (cid,)).fetchone()
        job = workspace._job(row) if row else workspace._fresh(cid)
        if data.get('revision') != job['revision']:
            raise workspace.Conflict('goal changed; refresh before editing')
        if db.execute("SELECT 1 FROM runs WHERE conversation_id=? AND state='running'", (cid,)).fetchone():
            raise workspace.Conflict('stop the current run before editing its plan')
        job['goal'] = _text(data.get('goal', ''), 2000)
        if not job['goal']:
            raise ValueError('goal required')
        constraints = data.get('constraints', [])
        steps = data.get('checkpoints', [])
        if not isinstance(constraints, list) or len(constraints) > 20 or not isinstance(steps, list) or len(steps) > 20:
            raise ValueError('at most 20 constraints or steps')
        job['constraints'] = [_text(x, 500) for x in constraints]
        job['checkpoints'] = []
        for step in steps:
            if not isinstance(step, dict) or step.get('status') not in ('pending', 'inProgress', 'completed'):
                raise ValueError('invalid checkpoint')
            job['checkpoints'].append({'step': _text(step.get('step', ''), 300), 'status': step['status']})
        # User goal disposition is independent of recorded execution outcome.
        disposition = data.get('disposition', 'active')
        if disposition not in ('active', 'paused', 'closed'):
            raise ValueError('invalid goal disposition')
        job['disposition'] = disposition
        return workspace._save(db, job)


def objects(cid):
    with workspace.database() as db:
        _setup(db)
        rows = db.execute('''SELECT a.* FROM app_objects a WHERE conversation_id=?
            AND revision=(SELECT MAX(b.revision) FROM app_objects b WHERE b.id=a.id)
            ORDER BY updated DESC LIMIT 100''', (cid,))
        return [dict(json.loads(r['body']), id=r['id'], kind=r['kind'], revision=r['revision'], updated=r['updated']) for r in rows]


def object_version(cid, oid, revision):
    with workspace.database() as db:
        _setup(db)
        row = db.execute('SELECT * FROM app_objects WHERE conversation_id=? AND id=? AND revision=?', (cid, oid, revision)).fetchone()
        if not row:
            raise ValueError('version not found')
        return dict(json.loads(row['body']), id=oid, kind=row['kind'], revision=row['revision'])


def save_object(cid, data):
    kind = data.get('kind')
    if kind not in ('artifact', 'recipe'):
        raise ValueError('invalid object kind')
    oid = str(data.get('id') or '')
    if not oid or len(oid) > 80 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in oid):
        raise ValueError('invalid object ID')
    value = {'title': _text(data.get('title', ''), 200)}
    if not value['title']:
        raise ValueError('title required')
    if kind == 'recipe':
        value['prompt'] = _text(data.get('prompt', ''), 3500)
        value['origin'] = 'agent' if data.get('origin') == 'agent' else 'user'
        if not isinstance(data.get('archived', False), bool):
            raise ValueError('invalid archived state')
        value['archived'] = data.get('archived', False)
        if not value['prompt']:
            raise ValueError('instructions required')
    else:
        value['description'] = _text(data.get('description', ''), 4000)
        columns, rows = data.get('columns', []), data.get('rows', [])
        if not isinstance(columns, list) or not 1 <= len(columns) <= 8 or not isinstance(rows, list) or len(rows) > 200:
            raise ValueError('use 1–8 columns and at most 200 rows')
        value['columns'] = [_text(c, 100) for c in columns]
        value['rows'] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('cells'), list) or len(row['cells']) != len(columns) or not isinstance(row.get('done', False), bool):
                raise ValueError('invalid row')
            value['rows'].append({'cells': [_text(c, 1000) for c in row['cells']], 'done': row.get('done', False)})
    with workspace.database() as db:
        _setup(db)
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM deleted_conversations WHERE id=?', (cid,)).fetchone():
            raise workspace.Conflict('conversation was deleted')
        previous = db.execute('SELECT * FROM app_objects WHERE id=? ORDER BY revision DESC LIMIT 1', (oid,)).fetchone()
        revision = previous['revision'] if previous else 0
        if previous and (previous['conversation_id'] != cid or previous['kind'] != kind):
            raise workspace.Conflict('object is not in this conversation')
        if data.get('revision') != revision:
            # A retry of the same completed save returns its recorded revision.
            if previous and data.get('revision') == revision - 1 and json.loads(previous['body']) == value:
                return dict(value, id=oid, kind=kind, revision=revision)
            raise workspace.Conflict('object changed; reload before saving')
        db.execute('INSERT INTO app_objects VALUES(?,?,?,?,?,?)', (oid, cid, kind, revision + 1, time.time(), json.dumps(value, ensure_ascii=False)))
        return dict(value, id=oid, kind=kind, revision=revision + 1)
