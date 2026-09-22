import io

import pytest

import operator_workspace as ws


def test_job_survives_runs_but_late_updates_cannot():
    first = ws.start_run('a', 'Find a tasting')
    ws.update_job(first['run_id'], first['credential'], {
        'constraints': ['Tuesday', 'two adults'],
        'checkpoints': [{'step': 'Check dates', 'status': 'inProgress'}]})
    ws.finish_run(first['run_id'], 'done')
    second = ws.start_run('a', 'Earliest available')
    assert second['job']['id'] == first['job']['id']
    assert second['job']['constraints'] == ['Tuesday', 'two adults']
    with pytest.raises(ws.Conflict):
        ws.update_job(first['run_id'], first['credential'], {'goal': 'stale'})
    with pytest.raises(PermissionError):
        ws.update_job(second['run_id'], first['credential'], {'goal': 'foreign'})
    assert 'two adults' in ws.context('a')
    assert ws.snapshot('b')['job'] is None


def test_new_job_preserves_prior_results_and_changes_run_owner():
    run = ws.start_run('a', 'First')
    next_job = ws.update_job(run['run_id'], run['credential'], {'new_job': True, 'goal': 'Second'})
    assert next_job['id'] != run['job']['id']
    assert len(ws.snapshot('a')['jobs']) == 2
    assert ws.run_state(run['run_id'], run['credential'])['job']['goal'] == 'Second'


def test_confirmed_result_requires_actual_evidence():
    run = ws.start_run('a', 'Get a file')
    with pytest.raises(ValueError):
        ws.publish_result(run['run_id'], run['credential'], {'status': 'confirmed'})
    ev = ws.observe(run['run_id'], 'browser_snapshot', 'Confirmation ABC', ['https://example.com/receipt'])
    result = ws.publish_result(run['run_id'], run['credential'], {
        'title': 'Booking', 'status': 'confirmed', 'confirmation': 'ABC', 'evidence_ids': [ev['id']]})
    assert result['status'] == 'confirmed'
    with pytest.raises(ValueError):
        ws.publish_result(run['run_id'], run['credential'], {'evidence_ids': ['made-up']})


def test_approval_is_exact_and_revision_invalidates_pending():
    run = ws.start_run('a', 'Book')
    args = {'kind': 'booking', 'destination': 'example.com', 'description': 'Tuesday at 10'}
    first = ws.request_approval(run['run_id'], run['credential'], args)
    assert first['status'] == 'pending'
    assert ws.request_approval(run['run_id'], run['credential'], args)['id'] == first['id']
    second = ws.request_approval(run['run_id'], run['credential'], dict(args, description='Wednesday at 10'))
    with pytest.raises(ws.Conflict):
        ws.decide_approval('a', first['id'], first['fingerprint'], True)
    with pytest.raises(KeyError):
        ws.decide_approval('b', second['id'], second['fingerprint'], True)
    assert ws.decide_approval('a', second['id'], second['fingerprint'], True)['status'] == 'approved'


def test_recipe_authorization_is_bounded_and_cumulative():
    policy = {'mode': 'bounded', 'actions': ['purchase'], 'destinations': ['shop.test'],
              'max_amount': 30, 'currency': 'CAD'}
    run = ws.start_run('a', 'Buy', policy)
    args = {'kind': 'purchase', 'destination': 'shop.test', 'description': 'One book', 'amount': 20, 'currency': 'CAD'}
    assert ws.request_approval(run['run_id'], run['credential'], args)['status'] == 'approved'
    assert ws.request_approval(run['run_id'], run['credential'], dict(args, description='Second book'))['status'] == 'pending'
    assert ws.request_approval(run['run_id'], run['credential'], dict(args, destination='other.test'))['status'] == 'pending'
    with pytest.raises(ValueError):
        ws.clean_authorization(dict(policy, max_amount=float('nan')))


def test_file_ownership_limits_and_chat_deletion(monkeypatch):
    asset = ws.put_file('a', '../../report.txt', io.BytesIO(b'report'))
    path, info = ws.file_path('a', asset['id'])
    assert path.read_bytes() == b'report' and info['name'] == 'report.txt'
    with pytest.raises(KeyError):
        ws.file_path('b', asset['id'])
    with pytest.raises(KeyError):
        ws.file_path('a', '../../secret')
    monkeypatch.setenv('OPERATOR_FILE_LIMIT_MIB', '1')
    with pytest.raises(ValueError):
        ws.put_file('a', 'large', io.BytesIO(b'x' * (1024 * 1024 + 1)))
    assert len(list((ws.root() / 'files').iterdir())) == 1
    ws.delete_conversation('a')
    assert not path.exists()


def test_correction_deduplicates_and_is_conversation_scoped():
    run = ws.start_run('a', 'Task')['run_id']
    first, fresh = ws.correction('a', run, 'msg', 'Tuesday')
    assert fresh
    ws.correction_status('msg', 'delivered')
    second, fresh = ws.correction('a', run, 'msg', 'Tuesday')
    assert not fresh and second['status'] == 'delivered'
    with pytest.raises(ws.Conflict):
        ws.correction('b', run, 'msg', 'Tuesday')


def test_stopped_run_cannot_accept_new_correction():
    run = ws.start_run('a', 'Task')
    ws.finish_run(run['run_id'], 'interrupted')
    with pytest.raises(ws.Conflict):
        ws.correction('a', run['run_id'], 'late', 'Tuesday')


def test_approval_survives_resume_once_without_resetting_budget():
    policy = {'mode': 'bounded', 'actions': ['purchase'], 'destinations': ['shop.test'],
              'max_amount': 30, 'currency': 'CAD'}
    run = ws.start_run('a', 'Buy', policy)
    args = {'kind': 'purchase', 'destination': 'shop.test', 'description': 'Book', 'amount': 20, 'currency': 'CAD'}
    ws.request_approval(run['run_id'], run['credential'], args)
    ws.finish_run(run['run_id'], 'done')
    again = ws.start_run('a', 'Another book')
    grant = ws.request_approval(again['run_id'], again['credential'], dict(args, description='Second book'))
    assert grant['status'] == 'pending'
    ws.finish_run(again['run_id'], 'done')
    ws.decide_approval('a', grant['id'], grant['fingerprint'], True)
    resumed = ws.start_run('a', 'Continue')
    claimed = ws.request_approval(resumed['run_id'], resumed['credential'], dict(args, description='Second book'))
    assert claimed['id'] == grant['id'] and claimed['claimed']
    assert ws.snapshot('a')['job']['approved_amount'] == 40


def test_deletion_removes_owned_generated_files_and_partial_downloads():
    import operator_file_bridge as bridge
    run = ws.start_run('a', 'File')
    folder = ws.root() / 'artifacts' / run['run_id']
    folder.mkdir(parents=True)
    (folder / 'unfinished.txt').write_text('fixture')
    guid = 'a' * 32
    with ws.database() as db:
        db.execute('INSERT INTO transfers VALUES (?,?,?,?,?,?)', (guid, 'a', 'part', 'downloading', 0, ''))
    partial = bridge.staging()[0] / (guid + '.crdownload')
    partial.write_bytes(b'partial')
    ws.finish_run(run['run_id'], 'done')
    ws.delete_conversation('a')
    assert not folder.exists() and not partial.exists()


def test_startup_recovers_stale_run_without_losing_job():
    ws.start_run('a', 'Persistent goal')
    ws.recover_runs()
    assert ws.snapshot('a')['job']['state'] == 'interrupted'
    assert ws.start_run('a', 'Continue')['job']['goal'] == 'Persistent goal'
