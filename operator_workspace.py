"""Durable, conversation-owned jobs and artifacts. Never store these in frame caches.

Browser session HTML remains a presentation cache, not the authority for this data.
Run credentials fence late MCP updates after Stop, replacement, or chat deletion.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
from urllib.parse import urlsplit
import uuid


class Conflict(ValueError):
    pass


def root() -> Path:
    path = Path(os.environ.get('OPERATOR_WORKSPACE_DIR',
                    str(Path.home() / '.local/share/operator/workspace')))
    if os.environ.get('OPERATOR_DEMO') == '1':
        path = path / 'demo'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextlib.contextmanager
def database():
    db = sqlite3.connect(root() / 'workspace.sqlite3', timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version > 1:
        db.close()
        raise RuntimeError('workspace schema is newer than this Operator build')
    if version == 0:
      db.executescript('''
      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
        revision INTEGER NOT NULL, updated REAL NOT NULL, body TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS jobs_conversation ON jobs(conversation_id, updated);
      CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
        credential TEXT NOT NULL, state TEXT NOT NULL, started REAL NOT NULL, ended REAL);
      CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, name TEXT NOT NULL,
        size INTEGER NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL,
        source TEXT NOT NULL, created REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS corrections (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, run_id TEXT NOT NULL,
        text TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS transfers (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, name TEXT NOT NULL,
        status TEXT NOT NULL, size INTEGER NOT NULL, error TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS deleted_conversations (id TEXT PRIMARY KEY);
      PRAGMA user_version=1;
    ''')
    os.chmod(root() / 'workspace.sqlite3', 0o600)
    try:
        with db:
            yield db
    finally:
        db.close()


def _text(value, limit=1000):
    if not isinstance(value, str):
        raise ValueError('expected text')
    return value.strip()[:limit]


def _job(row):
    if row is None:
        return None
    return dict(json.loads(row['body']), id=row['id'], revision=row['revision'],
                conversation_id=row['conversation_id'], updated=row['updated'])


def _save(db, job):
    job = dict(job)
    jid, cid = job.pop('id'), job.pop('conversation_id')
    rev = job.pop('revision', 0) + 1
    job.pop('updated', None)
    db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?)',
               (jid, cid, rev, time.time(), json.dumps(job, ensure_ascii=False)))
    return _job(db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())


def _fresh(cid, goal=''):
    return dict(id=uuid.uuid4().hex, conversation_id=cid, revision=0,
                goal=_text(goal, 2000), constraints=[], checkpoints=[], decisions=[],
                results=[], evidence=[], approvals=[], state='idle', active_run='',
                authorization={'mode': 'confirm'})


def snapshot(cid):
    with database() as db:
        jobs = [_job(row) for row in db.execute(
            'SELECT * FROM jobs WHERE conversation_id=? ORDER BY updated DESC LIMIT 50', (cid,))]
        files = [dict(row) for row in db.execute(
            "SELECT * FROM files WHERE conversation_id=? AND status='ready' ORDER BY created DESC", (cid,))]
        corrections = [dict(row) for row in db.execute(
            'SELECT id,run_id,status,created FROM corrections WHERE conversation_id=? ORDER BY created DESC LIMIT 20', (cid,))]
        total = db.execute('SELECT COALESCE(SUM(size),0) FROM files').fetchone()[0]
        transfers = [dict(r) for r in db.execute("SELECT * FROM transfers WHERE conversation_id=? AND status!='ready' ORDER BY rowid DESC LIMIT 20", (cid,))]
    return dict(job=jobs[0] if jobs else None, jobs=jobs, files=files,
                transfers=transfers, corrections=corrections, storage={'chat': sum(f['size'] for f in files),
                'total': total, 'file_limit': limit('FILE', 100),
                'chat_limit': limit('CHAT', 1024), 'total_limit': limit('TOTAL', 10240)})


def new_job(cid, goal=''):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM deleted_conversations WHERE id=?', (cid,)).fetchone():
            raise Conflict('this chat was deleted')
        if db.execute("SELECT 1 FROM runs WHERE conversation_id=? AND state='running'", (cid,)).fetchone():
            raise Conflict('stop the current run before starting a new job')
        previous = _job(db.execute('SELECT * FROM jobs WHERE conversation_id=? ORDER BY updated DESC LIMIT 1', (cid,)).fetchone())
        if previous:
            for approval in previous['approvals']:
                if approval['status'] == 'pending' or (approval['status'] == 'approved' and not approval.get('claimed')):
                    approval['status'] = 'superseded'
            _save(db, previous)
        return _save(db, _fresh(cid, goal))


def start_run(cid, task, authorization=None):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM deleted_conversations WHERE id=?', (cid,)).fetchone():
            raise Conflict('this chat was deleted')
        if db.execute("SELECT 1 FROM runs WHERE conversation_id=? AND state='running'", (cid,)).fetchone():
            raise Conflict('this conversation already has an active run')
        previous = _job(db.execute('SELECT * FROM jobs WHERE conversation_id=? ORDER BY updated DESC LIMIT 1', (cid,)).fetchone())
        if authorization is not None and previous:
            if any(a['status'] == 'pending' for a in previous['approvals']):
                raise Conflict('finish or decline the pending approval before running another recipe')
            job = _fresh(cid, task)  # each explicitly started recipe owns its budget
        else:
            job = previous or _fresh(cid, task)
        if not job['goal']:
            job['goal'] = _text(task, 2000)
        if authorization is not None:
            job['authorization'] = clean_authorization(authorization)
        run_id, credential = uuid.uuid4().hex, secrets.token_urlsafe(32)
        job.update(active_run=run_id, active_task=_text(task, 2000), state='running')
        db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,NULL)',
                   (run_id, job['id'], cid, hashlib.sha256(credential.encode()).hexdigest(), 'running', time.time()))
        return dict(job=_save(db, job), run_id=run_id, credential=credential)


def _owned_run(db, run_id, credential=None):
    run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    if not run or run['state'] != 'running':
        raise Conflict('run is no longer active')
    if credential is not None and not secrets.compare_digest(
            run['credential'], hashlib.sha256(credential.encode()).hexdigest()):
        raise PermissionError('run credential does not match')
    job = _job(db.execute('SELECT * FROM jobs WHERE id=?', (run['job_id'],)).fetchone())
    if not job or job['active_run'] != run_id:
        raise Conflict('job has moved to another run')
    return run, job


def finish_run(run_id, state):
    if not run_id:
        return
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            run, job = _owned_run(db, run_id)
        except Conflict:
            return
        db.execute('UPDATE runs SET state=?,ended=? WHERE id=?', (state, time.time(), run_id))
        db.execute("UPDATE corrections SET status='failed' WHERE run_id=? AND status IN ('pending','accepted')", (run_id,))
        job['state'] = 'needs_input' if any(a['status'] == 'pending' for a in job['approvals']) else state
        _save(db, job)


def recover_runs():
    """Called once by server startup, never by an MCP child importing the store."""
    with database() as db:
        ids = [r[0] for r in db.execute("SELECT id FROM runs WHERE state='running'")]
    for rid in ids:
        finish_run(rid, 'interrupted')


def context(cid):
    state = snapshot(cid)
    job = state['job']
    if not job:
        return ''
    compact = {k: job[k] for k in ('goal', 'authorization')}
    compact['constraints'] = job['constraints'][:8]
    compact['checkpoints'] = job['checkpoints'][:10]
    compact['decisions'] = job['decisions'][-5:]
    compact['approvals'] = job['approvals'][-3:]
    compact['files'] = [{'id': f['id'], 'name': f['name']} for f in state['files'][:20]]
    return 'Current Operator job (persisted across devices; job_state has the full record):\n' + json.dumps(compact, ensure_ascii=False)


def update_job(run_id, credential, fields):
    if not isinstance(fields, dict):
        raise ValueError('job update must be an object')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        run, job = _owned_run(db, run_id, credential)
        if fields.get('new_job'):
            for approval in job['approvals']:
                if approval['status'] == 'pending' or (approval['status'] == 'approved' and not approval.get('claimed')):
                    approval['status'] = 'superseded'
            job['state'] = 'done'
            _save(db, job)
            job = _fresh(run['conversation_id'], fields.get('goal', ''))
            job.update(active_run=run_id, state='running')
            db.execute('UPDATE runs SET job_id=? WHERE id=?', (job['id'], run_id))
        if 'goal' in fields:
            job['goal'] = _text(fields['goal'], 2000)
        for key in ('constraints', 'decisions'):
            if key in fields:
                if not isinstance(fields[key], list) or len(fields[key]) > 20:
                    raise ValueError(key + ' must contain at most 20 entries')
                job[key] = [_text(v, 500) for v in fields[key]]
        if 'checkpoints' in fields:
            steps = fields['checkpoints']
            if not isinstance(steps, list) or len(steps) > 20:
                raise ValueError('at most 20 checkpoints')
            clean = []
            for step in steps:
                if not isinstance(step, dict) or step.get('status') not in ('pending', 'inProgress', 'completed'):
                    raise ValueError('checkpoint status must be pending, inProgress, or completed')
                clean.append({'step': _text(step.get('step', ''), 300), 'status': step['status']})
            job['checkpoints'] = clean
        return _save(db, job)


def observe(run_id, tool, detail='', urls=()):
    """Trusted runner records evidence from completed tool output, not agent claims."""
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        _, job = _owned_run(db, run_id)
        evidence = {'id': uuid.uuid4().hex, 'tool': _text(tool, 100),
                    'detail': _text(detail, 1500), 'observed': time.time(),
                    'urls': [u for u in urls if isinstance(u, str) and urlsplit(u).scheme in ('http', 'https')][:10]}
        job['evidence'] = (job['evidence'] + [evidence])[-80:]
        _save(db, job)
        return evidence


def publish_result(run_id, credential, result):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        run, job = _owned_run(db, run_id, credential)
        status = result.get('status', 'found')
        if status not in ('found', 'prepared', 'confirmed'):
            raise ValueError('invalid result status')
        refs = result.get('evidence_ids', [])
        if not isinstance(refs, list) or any(r not in {e['id'] for e in job['evidence']} for r in refs):
            raise ValueError('use evidence IDs returned by job_state')
        assets = result.get('file_ids', [])
        if not isinstance(assets, list) or len(assets) > 20:
            raise ValueError('invalid file IDs')
        for fid in assets:
            if not db.execute("SELECT 1 FROM files WHERE id=? AND conversation_id=? AND status='ready'",
                              (fid, run['conversation_id'])).fetchone():
                raise ValueError('file is not available in this conversation')
        confirmation = _text(result.get('confirmation', ''), 1000)
        if status == 'confirmed' and (not (refs or assets) or not confirmation):
            raise ValueError('confirmed results need observed evidence and confirmation details')
        value = {'id': uuid.uuid4().hex, 'run_id': run_id, 'task': job.get('active_task') or job['goal'],
                 'title': _text(result.get('title', ''), 180),
                 'summary': _text(result.get('summary', ''), 2500), 'status': status,
                 'confirmation': confirmation, 'evidence_ids': refs[:20],
                 'file_ids': assets, 'created': time.time()}
        job['results'] = (job['results'] + [value])[-30:]
        _save(db, job)
        return value


def clean_authorization(value):
    if not isinstance(value, dict) or value.get('mode', 'confirm') not in ('confirm', 'bounded'):
        raise ValueError('invalid authorization mode')
    if value.get('mode') != 'bounded':
        return {'mode': 'confirm'}
    actions = value.get('actions', [])
    destinations = value.get('destinations', [])
    if not isinstance(actions, list) or not actions or any(a not in ('purchase', 'booking', 'message') for a in actions):
        raise ValueError('select explicitly authorized actions')
    if not isinstance(destinations, list) or not destinations:
        raise ValueError('bounded authorization needs explicit destinations')
    maximum = value.get('max_amount', 0)
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or not 0 <= maximum <= 100000:
        raise ValueError('invalid authorization limit')
    currency = _text(value.get('currency', ''), 3).upper()
    if 'purchase' in actions and (not maximum or len(currency) != 3):
        raise ValueError('purchases need a total spending limit and currency')
    return {'mode': 'bounded', 'actions': actions, 'destinations': [_text(d, 200).lower() for d in destinations][:20],
            'max_amount': maximum, 'currency': currency}


def request_approval(run_id, credential, action):
    kind = action.get('kind')
    if kind not in ('purchase', 'booking', 'message', 'other'):
        raise ValueError('invalid action kind')
    amount = action.get('amount', 0)
    if isinstance(amount, bool) or not isinstance(amount, (float, int)) or not 0 <= amount <= 1000000:
        raise ValueError('invalid amount')
    proposal = {'kind': kind, 'destination': _text(action.get('destination', ''), 200).lower(),
                'description': _text(action.get('description', ''), 1500),
                'amount': amount, 'currency': _text(action.get('currency', ''), 3).upper()}
    if not proposal['destination'] or not proposal['description']:
        raise ValueError('describe the exact action and destination')
    fingerprint = hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        _, job = _owned_run(db, run_id, credential)
        for old in job['approvals']:
            if old['fingerprint'] == fingerprint and old['run_id'] == run_id:
                if old['status'] == 'approved' and not old.get('claimed'):
                    old['claimed'] = time.time()
                    _save(db, job)
                return old
            if (old['fingerprint'] == fingerprint and old['status'] == 'approved'
                    and old['source'] == 'user' and not old.get('claimed')):
                old.update(run_id=run_id, claimed=time.time())
                _save(db, job)
                return old
        # A revised proposal invalidates pending approvals, never reuses their grants.
        for old in job['approvals']:
            if old['status'] == 'pending' or (old['status'] == 'approved'
                    and old['source'] == 'user' and not old.get('claimed')):
                old['status'] = 'superseded'
        auth = job['authorization']
        used = job.get('approved_amount', sum(a['action']['amount'] for a in job['approvals'] if a['status'] == 'approved'))
        permitted = (auth.get('mode') == 'bounded' and kind in auth['actions']
                     and proposal['destination'] in auth['destinations']
                     and (kind == 'message' or 'amount' in action)
                     and (not amount or (proposal['currency'] == auth['currency']
                                         and used + amount <= auth['max_amount'])))
        item = dict(id=uuid.uuid4().hex, run_id=run_id, action=proposal,
                    fingerprint=fingerprint, status='approved' if permitted else 'pending',
                    source='recipe' if permitted else 'user', created=time.time())
        if permitted:
            item['claimed'] = time.time()
        job['approved_amount'] = used + (amount if permitted else 0)
        job['approvals'] = (job['approvals'] + [item])[-50:]
        _save(db, job)
        return item


def decide_approval(cid, approval_id, fingerprint, approved):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        job = _job(db.execute('SELECT * FROM jobs WHERE conversation_id=? ORDER BY updated DESC LIMIT 1', (cid,)).fetchone())
        if job:
            for item in job['approvals']:
                if item['id'] == approval_id:
                    if item['fingerprint'] != fingerprint or item['status'] != 'pending':
                        raise Conflict('this approval is no longer pending')
                    item['status'] = 'approved' if approved else 'declined'
                    item['decided'] = time.time()
                    if approved:
                        job['approved_amount'] = job.get('approved_amount', sum(
                            a['action']['amount'] for a in job['approvals']
                            if a['status'] == 'approved' and a['id'] != item['id'])) + item['action']['amount']
                    _save(db, job)
                    return item
        raise KeyError('approval not found')


def run_state(run_id, credential):
    with database() as db:
        run, _ = _owned_run(db, run_id, credential)
        cid = run['conversation_id']
    state = snapshot(cid)
    return {key: state[key] for key in ('job', 'files', 'storage', 'transfers')}


def limit(kind, default):
    return max(1, int(os.environ.get('OPERATOR_' + kind + '_LIMIT_MIB', default))) * 1024 * 1024


def put_file(cid, name, stream, source='upload'):
    # This also runs in MCP Python, which need not have Flask installed.
    # UUIDs, never display names, address durable blobs.
    name = _text(name, 240).replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[^\w. -]', '_', name).strip(' .') or 'file'
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', name.split('.')[0]):
        name = '_' + name
    fid = uuid.uuid4().hex
    folder = root() / 'files'
    folder.mkdir(exist_ok=True, mode=0o700)
    dest = folder / fid
    size, digest = 0, hashlib.sha256()
    try:
        with dest.open('xb') as handle:
            os.chmod(dest, 0o600)
            while block := stream.read(1024 * 1024):
                size += len(block)
                if size > limit('FILE', 100):
                    raise ValueError('file exceeds the configured size limit')
                digest.update(block)
                handle.write(block)
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM deleted_conversations WHERE id=?', (cid,)).fetchone():
                raise Conflict('this chat was deleted')
            used = db.execute('SELECT COALESCE(SUM(size),0) FROM files WHERE conversation_id=?', (cid,)).fetchone()[0]
            total = db.execute('SELECT COALESCE(SUM(size),0) FROM files').fetchone()[0]
            if used + size > limit('CHAT', 1024) or total + size > limit('TOTAL', 10240):
                raise ValueError('chat or total storage limit reached; remove files before uploading')
            db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?)',
                       (fid, cid, name, size, digest.hexdigest(), 'ready', source, time.time()))
        return {'id': fid, 'name': name, 'size': size, 'digest': digest.hexdigest()}
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def file_path(cid, fid):
    with database() as db:
        row = db.execute("SELECT * FROM files WHERE id=? AND conversation_id=? AND status='ready'", (fid, cid)).fetchone()
    if row is None:
        raise KeyError('file not found')
    path = root() / 'files' / row['id']
    if not path.is_file() or path.is_symlink():
        raise KeyError('file is unavailable')
    return path, dict(row)


def delete_file(cid, fid):
    path, _ = file_path(cid, fid)
    with database() as db:
        db.execute('DELETE FROM files WHERE id=? AND conversation_id=?', (fid, cid))
    path.unlink(missing_ok=True)
    import operator_file_bridge
    operator_file_bridge.clean_upload(fid)


def delete_conversation(cid):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM runs WHERE conversation_id=? AND state='running'", (cid,)).fetchone():
            raise Conflict('stop this conversation before deleting it')
        files = [r[0] for r in db.execute('SELECT id FROM files WHERE conversation_id=?', (cid,))]
        runs = [r[0] for r in db.execute('SELECT id FROM runs WHERE conversation_id=?', (cid,))]
        transfers = [r[0] for r in db.execute('SELECT id FROM transfers WHERE conversation_id=?', (cid,))]
        db.execute('INSERT OR IGNORE INTO deleted_conversations VALUES (?)', (cid,))
        db.execute("UPDATE transfers SET status='failed',error='Chat deleted' WHERE conversation_id=? AND status='downloading'", (cid,))
        for table in ('jobs', 'runs', 'files', 'corrections'):
            db.execute(f'DELETE FROM {table} WHERE conversation_id=?', (cid,))
    for fid in files:
        (root() / 'files' / fid).unlink(missing_ok=True)
        import operator_file_bridge
        operator_file_bridge.clean_upload(fid)
    import operator_file_bridge
    for guid in transfers:
        operator_file_bridge.clean_download(guid)
    artifact_root = root() / 'artifacts'
    if artifact_root.is_symlink():
        return  # never follow a substituted parent outside managed storage
    base = artifact_root.resolve()
    for run_id in runs:
        folder = base / run_id
        if folder.is_dir() and not folder.is_symlink() and folder.resolve().parent == base:
            shutil.rmtree(folder)


def correction(cid, run_id, message_id, text):
    message_id = _text(message_id, 80)
    if not message_id:
        raise ValueError('correction ID required')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM corrections WHERE id=?', (message_id,)).fetchone()
        if old:
            if old['conversation_id'] != cid or old['text'] != text:
                raise Conflict('correction ID was already used')
            return dict(old), False
        run, _ = _owned_run(db, run_id)
        if run['conversation_id'] != cid:
            raise Conflict('correction does not belong to this chat')
        db.execute('INSERT INTO corrections VALUES (?,?,?,?,?,?)',
                   (message_id, cid, run_id, _text(text, 4000), 'pending', time.time()))
        return dict(db.execute('SELECT * FROM corrections WHERE id=?', (message_id,)).fetchone()), True


def correction_status(message_id, status):
    if status not in ('pending', 'accepted', 'delivered', 'failed'):
        raise ValueError('invalid correction status')
    with database() as db:
        db.execute('UPDATE corrections SET status=? WHERE id=?', (status, message_id))
