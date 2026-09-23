"""Read model identities and selection policy from models.yaml."""
from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path
import yaml


@lru_cache(maxsize=1)
def load() -> dict:
    data = yaml.safe_load(Path(__file__).with_name('models.yaml').read_text())
    validate(data)
    return data


def validate(data: dict) -> None:
    if data.get('version') != 1:
        raise ValueError('unsupported model catalog version')
    models = data['models']
    slugs = set()
    for key, model in models.items():
        slug = model['slug']
        if slug in slugs:
            raise ValueError(f'duplicate model slug: {slug}')
        slugs.add(slug)
        if model['default_effort'] is not None and model['default_effort'] not in model['efforts']:
            raise ValueError(f'{key}: default effort is not supported')
        if model.get('retired_by') and model['retired_by'] not in models:
            raise ValueError(f'{key}: unknown retired successor')
        for rate in model.get('rates', {}).values():
            if rate is not None and (isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0):
                raise ValueError(f'{key}: invalid rate')
    for surface, rows in data['surfaces'].items():
        for row in rows:
            if row['key'] not in models or models[row['key']].get('retired_by'):
                raise ValueError(f'{surface}: invalid active model')
    for key in data['defaults'].values():
        if key not in models or models[key].get('retired_by'):
            raise ValueError('invalid default model')


def default_slug(name: str) -> str:
    data = load()
    return data['models'][data['defaults'][name]]['slug']


def aliases() -> dict[str, str]:
    return {m['alias']: m['slug'] for m in load()['models'].values() if m.get('alias')}


def runtime_specs() -> tuple[dict, ...]:
    return tuple({'key': key, 'label': m['label'], 'runtime': m['transport'],
                  'model': m['alias'] if m.get('runtime_alias') else m['slug'],
                  'efforts': tuple(m['efforts']), 'default_effort': m['default_effort']}
                 for key, m in load()['models'].items() if m.get('runtime'))


def legacy_model_keys() -> dict[str, str]:
    models = load()['models']
    return {m['slug']: m.get('retired_by', key) for key, m in models.items()
            if m.get('runtime') or models.get(m.get('retired_by'), {}).get('runtime')}


def pricing_tables() -> tuple[dict[str, tuple[float, float]], dict[str, float]]:
    rates, reads = {}, {}
    for m in load()['models'].values():
        p = m.get('rates')
        if not p or m.get('price_in_ledger') is False:
            continue
        match = m.get('rate_match', m['slug'].removeprefix('claude-'))
        rates[match] = (p['input'], p['output'])
        if p['cache_read'] != p['input'] * .1:
            reads[match] = p['cache_read']
    return rates, reads


def cache_write_rates(model: str, input_rate: float) -> tuple[float, float]:
    """Catalog rates, retaining the legacy estimate for an unknown model."""
    matches = []
    for entry in load()['models'].values():
        key = entry.get('rate_match', entry['slug'].removeprefix('claude-'))
        if key in (model or '').lower() and entry.get('rates'):
            matches.append((len(key), entry['rates']))
    rates = max(matches, key=lambda item: item[0])[1] if matches else {}
    return (rates.get('cache_write') if rates.get('cache_write') is not None else input_rate * 1.25,
            rates.get('cache_write_1h') if rates.get('cache_write_1h') is not None else input_rate * 2.0)


def picker(surface: str) -> list[dict]:
    data = load()
    rows = []
    for entry in data['surfaces'][surface]:
        m = data['models'][entry['key']]
        value = m.get('alias', m['slug']) if entry.get('value') == 'alias' else m['slug']
        label = m['label']
        if surface.startswith('operator-'):
            label = label.removeprefix('Claude ').removeprefix('Gemini ')
            label = label.replace('GPT ', 'GPT-')
        rows.append({'value': value, 'label': label})
    return rows


def effort_map() -> dict[str, list[str]]:
    result = {}
    models = load()['models']
    for m in models.values():
        current = models.get(m.get('retired_by'), m)
        result[m['slug']] = list(current['efforts'])
        if m.get('alias'):
            result[m['alias']] = list(m['efforts'])
    return result


def allowed_pins() -> set[str]:
    result = {'default', *load()['legacy_pins']}
    for m in load()['models'].values():
        result.update(m['slug'] + suffix for suffix in m.get('pin_variants', []))
    return result


def ticker_models() -> dict[str, dict]:
    data = load()
    return {entry['name']: {**entry['options'], 'id': data['models'][entry['key']]['slug'],
            'label': data['models'][entry['key']]['label'].removeprefix('Claude ').replace('GPT ', 'GPT-'),
            'cost_in': data['models'][entry['key']]['rates']['input'],
            'cost_out': data['models'][entry['key']]['rates']['output']}
            for entry in data['surfaces']['ticker']}
