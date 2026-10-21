"""Slash-picker catalog must not expose credentials or phantom connectors."""
from pathlib import Path
import pytest

from operator_resources import catalog


@pytest.fixture(autouse=True)
def isolated_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))


def test_catalog_only_advertises_enabled_codex_integrations(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('''
[mcp_servers.vecgrep]
command = "private-command"
env = { SECRET = "do-not-leak" }
[mcp_servers.disabled]
enabled = false
[mcp_servers.playwright]
command = "bash"
[plugins."github@openai-curated"]
enabled = true
[plugins."unused@openai-curated"]
enabled = false
''')
    resources = catalog("gpt", config_dir=str(tmp_path))
    assert any(r["name"] == "vecgrep" and r["kind"] == "mcp" for r in resources)
    assert any(r["name"] == "GitHub" and r["kind"] == "plugin" for r in resources)
    assert not any(r["name"] in {"disabled", "playwright", "unused"} for r in resources)
    assert "private-command" not in str(resources)
    assert "do-not-leak" not in str(resources)
    assert all(set(r) == {"kind", "name", "domain", "insert"} for r in resources)


def test_other_drivers_and_demo_do_not_claim_codex_integrations(tmp_path: Path):
    (tmp_path / "config.toml").write_text('[mcp_servers.vecgrep]\ncommand="x"\n')
    for bot, demo in (("claude-a", False), ("gemma", False), ("gpt", True)):
        resources = catalog(bot, demo=demo, config_dir=str(tmp_path))
        assert resources
        assert all(r["kind"] == "website" for r in resources)


def test_catalog_includes_food_travel_and_productivity_sites():
    domains = {r['domain'] for r in catalog('gemma', demo=True)}
    assert {'ubereats.com', 'doordash.com', 'instacart.com', 'resy.com',
            'alltrails.com', 'skyscanner.net', 'drive.google.com',
            'calendar.google.com', 'spotify.com'} <= domains


def test_gemma_catalog_includes_enabled_profile_mcps_without_connection_details(tmp_path: Path):
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'mcp_config.json').write_text('''{"mcpServers": {
        "lookup": {"serverUrl": "https://private.invalid", "headers": {"Authorization": "secret"}},
        "off": {"disabled": true}, "playwright": {"command": "internal"}}}''')
    items = catalog('gemma', config_dir=str(tmp_path))
    servers = [r for r in items if r['kind'] == 'mcp']
    assert [r['name'] for r in servers] == ['lookup']
    assert 'private.invalid' not in str(items)
    assert 'secret' not in str(items)


def test_configured_mcps_belong_to_operator_across_drivers(tmp_path: Path):
    profile = tmp_path / '.gemini' / 'config'
    profile.mkdir(parents=True)
    (profile / 'mcp_config.json').write_text(
        '{"mcpServers":{"lookup":{"command":"lookup-server"}}}')
    for bot in ('claude-a', 'gpt', 'gemma'):
        assert any(r['kind'] == 'mcp' and r['name'] == 'lookup' for r in catalog(bot))
