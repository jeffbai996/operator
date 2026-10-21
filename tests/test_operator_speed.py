"""Service tier availability comes from the installed runtime's model metadata."""
import json

import operator_speed as speed


def test_service_tiers_follow_the_cached_model_capabilities(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    profile = tmp_path / '.codex-operator'
    profile.mkdir()
    (profile / 'models_cache.json').write_text(json.dumps({'models': [
        {'slug': 'gpt-6.1-sol', 'service_tiers': [{'id': 'priority'}, {'id': 'ultrafast'}]},
        {'slug': 'gpt-6-luna', 'service_tiers': [{'id': 'priority'}]}]}))
    assert speed.tiers_for('gpt-6.1-sol') == ['default', 'priority', 'ultrafast']
    assert speed.tiers_for('gpt-6-luna') == ['default', 'priority']
    assert speed.tiers_for('unknown-model') == ['default']


def test_invalid_or_unsupported_speed_is_refused_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    assert speed.error('gpt', 'gpt-6.1-sol', 'turbo')
    assert speed.error('gpt', 'unknown-model', 'ultrafast')
    assert speed.error('gemma', 'gemini-4-argon', 'priority')
    assert speed.error('gpt', 'gpt-6.1-sol', 'ultrafast') is None
