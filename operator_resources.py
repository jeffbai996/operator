"""Connector catalog and configured MCPs for Operator's runtime profiles."""
from __future__ import annotations

import os
import json
import re
import tomllib


_SITES = (
    ("Google", "google.com"), ("Google Maps", "maps.google.com"),
    ("Gmail", "mail.google.com"), ("GitHub", "github.com"),
    ("YouTube", "youtube.com"), ("Wikipedia", "wikipedia.org"),
    ("Booking.com", "booking.com"), ("Expedia", "expedia.com"),
    ("Airbnb", "airbnb.com"), ("Amazon", "amazon.com"),
    ("Reddit", "reddit.com"), ("OpenTable", "opentable.com"),
    ("Uber Eats", "ubereats.com"), ("DoorDash", "doordash.com"),
    ("Instacart", "instacart.com"), ("Resy", "resy.com"),
    ("Google Drive", "drive.google.com"), ("Google Calendar", "calendar.google.com"),
    ("Google Flights", "flights.google.com"), ("Google Docs", "docs.google.com"),
    ("Skyscanner", "skyscanner.net"), ("Kayak", "kayak.com"),
    ("Tripadvisor", "tripadvisor.com"), ("Trip.com", "trip.com"),
    ("Trivago", "trivago.com"), ("Priceline", "priceline.com"),
    ("Turo", "turo.com"), ("Uber", "uber.com"),
    ("AllTrails", "alltrails.com"), ("Spotify", "spotify.com"),
    ("Notion", "notion.so"), ("Linear", "linear.app"),
    ("Slack", "slack.com"), ("Trello", "trello.com"),
    ("LinkedIn", "linkedin.com"), ("Yelp", "yelp.com"),
    ("Etsy", "etsy.com"), ("eBay", "ebay.com"),
    ("Costco", "costco.com"), ("Target", "target.com"),
    ("IKEA", "ikea.com"), ("Walmart", "walmart.com"),
    ("Realtor.com", "realtor.com"), ("Zillow", "zillow.com"),
    ("Redfin", "redfin.com"), ("Weather", "weather.gov"),
    ("Stack Overflow", "stackoverflow.com"), ("arXiv", "arxiv.org"),
)
_PLUGIN_DOMAINS = {
    "github": "github.com", "google-drive": "drive.google.com",
    "gmail": "mail.google.com", "slack": "slack.com",
    "notion": "notion.so", "linear": "linear.app",
}
_PLUGIN_LABELS = {"github": "GitHub", "google-drive": "Google Drive",
                  "gmail": "Gmail"}
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,79}$")


def configured_mcps(bot: str, config_dir: str) -> dict:
    """Read server definitions locally; callers must never serialize them to UI."""
    if bot == 'gpt':
        paths = [os.path.join(config_dir, 'config.toml')]
    elif bot == 'gemma':
        paths = [os.path.join(config_dir, 'config', 'mcp_config.json')]
    else:
        paths = [os.path.join(config_dir, '.claude.json')]
        if os.path.basename(config_dir) == '.claude':
            paths.insert(0, os.path.join(os.path.dirname(config_dir), '.claude.json'))
    servers = {}
    for path in paths:
        try:
            with open(path, 'rb') as handle:
                config = tomllib.load(handle) if path.endswith('.toml') else json.load(handle)
            raw = config.get('mcp_servers' if bot == 'gpt' else 'mcpServers', {})
            if isinstance(raw, dict):
                servers.update({name: detail for name, detail in raw.items()
                    if isinstance(name, str) and _NAME.fullmatch(name)
                    and isinstance(detail, dict) and detail.get('enabled') is not False
                    and not detail.get('disabled', False)})
        except (OSError, ValueError, AttributeError):
            continue
    return servers


def shared_mcps(bot: str = '', config_dir: str = '') -> dict:
    """Operator's connector set is shared; each adapter translates transport keys."""
    profiles = [('gpt', '~/.codex'), ('gpt', '~/.codex-operator'), ('claude-a', '~/.claude'),
                ('claude-b', '~/.config/claude-b'), ('gemma', '~/.gemini')]
    if config_dir:
        profiles.append((bot, config_dir))
    result = {}
    for driver, path in profiles:
        for name, detail in configured_mcps(driver, os.path.expanduser(path)).items():
            if name in {'playwright', 'operator-control', 'operator'}:
                continue
            if detail.get('command'):
                result[name] = {k: detail[k] for k in ('command', 'args', 'env') if k in detail}
            elif detail.get('url') or detail.get('serverUrl'):
                value = {'url': detail.get('url') or detail['serverUrl']}
                headers = dict(detail.get('http_headers') or detail.get('headers') or {})
                for key, env_name in detail.get('env_http_headers', {}).items():
                    if os.environ.get(env_name):
                        headers[key] = os.environ[env_name]
                token_name = detail.get('bearer_token_env_var')
                if token_name and os.environ.get(token_name):
                    headers['Authorization'] = 'Bearer ' + os.environ[token_name]
                if headers:
                    value['headers'] = headers
                result[name] = value
    return result


def catalog(bot: str, *, demo: bool = False, config_dir: str = "") -> list[dict]:
    """Return display metadata only. Never return MCP commands, URLs, or env."""
    items = [dict(kind="website", name=name, domain=domain,
                  insert=f"https://{domain}") for name, domain in _SITES]
    if demo:
        return items
    profile = config_dir or os.path.expanduser(
        '~/.codex-operator' if bot == 'gpt' else '~/.gemini' if bot == 'gemma' else '~/.claude')
    for name in sorted(shared_mcps(bot, profile)):
        if name not in {'playwright', 'operator-control', 'operator'}:
            items.append(dict(kind='mcp', name=name, domain='', insert=f'the {name} MCP'))
    if bot != 'gpt':
        return items
    path = os.path.join(profile,
                        "config.toml")
    try:
        with open(path, "rb") as handle:
            config = tomllib.load(handle)
    except (OSError, ValueError):
        return items

    plugins = config.get("plugins", {})
    if isinstance(plugins, dict):
        for key, detail in sorted(plugins.items()):
            if (not isinstance(key, str) or not _NAME.fullmatch(key)
                    or not isinstance(detail, dict)
                    or detail.get("enabled") is False):
                continue
            slug = key.split("@", 1)[0]
            label = _PLUGIN_LABELS.get(slug, slug.replace("-", " ").title())
            items.append(dict(kind="plugin", name=label,
                              domain=_PLUGIN_DOMAINS.get(slug, ""),
                              insert=f"the {label} plugin"))
    return items
