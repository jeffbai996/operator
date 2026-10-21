"""Supported processing tiers for Operator's GPT runtime."""
from __future__ import annotations

import json
from pathlib import Path


def tiers_for(model: str) -> list[str]:
    """Use advertised account capabilities, with documented current fallbacks."""
    for profile in ('.codex-operator', '.codex'):
        try:
            data = json.loads((Path.home() / profile / 'models_cache.json').read_text())
            for item in data.get('models', []):
                if item.get('slug') == model:
                    tiers = {tier.get('id') for tier in item.get('service_tiers', [])}
                    return ['default'] + [tier for tier in ('priority', 'ultrafast') if tier in tiers]
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return ['default', 'priority', 'ultrafast'] if model in {'gpt-6.1-sol', 'gpt-6-astra'} else ['default']


def error(bot: str, model: str, tier: str) -> str | None:
    if not tier:
        return None
    if bot != 'gpt':
        return 'Processing speed is available for GPT models.'
    if tier not in tiers_for(model):
        return f'Processing speed {tier!r} is unavailable for {model}.'
    return None
