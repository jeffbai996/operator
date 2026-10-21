import pytest
import operator_workspace as workspace
import operator_app_workspace as app


def test_goal_edit_is_versioned_and_does_not_dispatch():
    job = app.goal('a', {'revision': 0, 'goal': 'Plan a trip', 'constraints': ['Under $1000'], 'checkpoints': [{'step': 'Choose dates', 'status': 'pending'}]})
    assert job['state'] == 'idle'
    assert workspace.snapshot('a')['job']['goal'] == 'Plan a trip'
    with pytest.raises(workspace.Conflict):
        app.goal('a', {'revision': 0, 'goal': 'Stale write'})
    run = workspace.start_run('a', 'Research')
    with pytest.raises(workspace.Conflict):
        app.goal('a', {'revision': run['job']['revision'], 'goal': 'Replace'})
    workspace.finish_run(run['run_id'], 'stopped')
    current = workspace.snapshot('a')['job']
    paused = app.goal('a', {'revision': current['revision'], 'goal': current['goal'], 'disposition': 'paused'})
    assert paused['state'] == 'stopped' and paused['disposition'] == 'paused'


def test_artifact_versions_ownership_and_retry():
    data = {'id': 'artifact-123', 'revision': 0, 'kind': 'artifact', 'title': 'Trip', 'columns': ['Day', 'Plan'], 'rows': [{'cells': ['Monday', '<script>literal</script>'], 'done': False}]}
    first = app.save_object('a', data)
    assert app.save_object('a', data)['revision'] == 1
    changed = dict(data, revision=1, rows=[{'cells': ['Monday', 'Museum'], 'done': True}])
    assert app.save_object('a', changed)['revision'] == 2
    assert not app.object_version('a', first['id'], 1)['rows'][0]['done']
    assert app.objects('b') == []
    with pytest.raises(ValueError):
        app.object_version('b', first['id'], 1)
    with pytest.raises(workspace.Conflict):
        app.save_object('b', changed)
    with pytest.raises(workspace.Conflict):
        app.save_object('a', dict(changed, title='Stale edit'))


def test_invalid_artifact_does_not_write():
    data = {'id': 'artifact-123', 'revision': 0, 'kind': 'artifact', 'title': 'Trip', 'columns': ['One'], 'rows': [{'cells': ['One', 'Two']}]}
    with pytest.raises(ValueError):
        app.save_object('a', data)
    assert app.objects('a') == []


def test_recipe_is_definition_not_execution():
    value = app.save_object('a', {'id': 'recipe-123', 'revision': 0, 'kind': 'recipe', 'title': 'Research', 'prompt': 'Compare the supplied options'})
    assert value['prompt'] and workspace.snapshot('a')['jobs'] == []
    assert app.objects('a')[0]['kind'] == 'recipe'


def test_conversation_deletion_removes_saved_object_versions():
    app.save_object('a', {'id': 'recipe-123', 'revision': 0, 'kind': 'recipe', 'title': 'Research', 'prompt': 'Compare the supplied options'})
    workspace.delete_conversation('a')
    assert app.objects('a') == []
    with pytest.raises(ValueError):
        app.object_version('a', 'recipe-123', 1)
