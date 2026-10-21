"""Read model identities and selection policy from models.yaml."""
from __future__ import annotations

from functools import lru_cache
import math
import re
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
            if row.get('default_effort') and row['default_effort'] not in models[row['key']]['efforts']:
                raise ValueError(f'{surface}: default effort is not supported')
    for key in data['defaults'].values():
        if key not in models or models[key].get('retired_by'):
            raise ValueError('invalid default model')
    for key, model in models.items():
        if model.get('fallback') and not models.get(model['fallback'], {}).get('runtime'):
            raise ValueError(f'{key}: fallback must name a runtime model')
        if model['transport'] == 'llamacpp' and not model.get('endpoint'):
            raise ValueError(f'{key}: a llamacpp model names its endpoint')


def model_slug(key: str) -> str:
    return load()['models'][key]['slug']


def default_slug(name: str) -> str:
    data = load()
    return data['models'][data['defaults'][name]]['slug']


def aliases() -> dict[str, str]:
    return {m['alias']: m['slug'] for m in load()['models'].values() if m.get('alias')}


# Per-model runtime facts a transport may need: where it is served, how much
# it may say, how long to wait, and which runtime model stands in for it.
RUNTIME_OPTIONS = ('endpoint', 'max_output_tokens', 'timeout_s', 'fallback')


def runtime_specs() -> tuple[dict, ...]:
    return tuple({'key': key, 'label': m['label'], 'runtime': m['transport'],
                  'model': m['alias'] if m.get('runtime_alias') else m['slug'],
                  'efforts': tuple(m['efforts']), 'default_effort': m['default_effort'],
                  **{opt: m[opt] for opt in RUNTIME_OPTIONS if opt in m}}
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
    if not matches:
        return input_rate * 1.25, input_rate * 2.0
    rates = max(matches, key=lambda item: item[0])[1]
    # A known model without a separate write tariff uses ordinary input cost.
    # No duration-specific tariff means no invented one-hour surcharge.
    write = rates.get('cache_write')
    write = input_rate if write is None else write
    hour = rates.get('cache_write_1h')
    return write, write if hour is None else hour


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


def discord_label(label: str) -> str:
    """A model's name in every Discord bot menu.

    The full catalog name with its vendor, so a menu that mixes vendors (gem's
    agy list) reads the same as a single-vendor one, and OpenAI's own GPT-N
    hyphenation.
    """
    return re.sub(r'^GPT (\d)', r'GPT-\1', label)


def gpt_bot_models() -> list[dict]:
    """Model picker for the GPT Discord bot."""
    data = load()
    models = data['models']
    return [{'name': models[row['key']]['label'].removeprefix('GPT '), 'value': models[row['key']]['slug']}
            for row in data['surfaces']['gpt']]


def claude_bot_models() -> dict:
    """Picker, default and saved-value map for the Claude Discord bot."""
    data = load()
    models = data['models']
    keys = [row['key'] for row in data['surfaces']['claude-bot']]
    slugs = {models[k]['slug'] for k in keys}
    retired = {models[k]['alias']: models[k]['slug'] for k in keys if models[k].get('alias')}
    retired.update({m['slug']: models[m['retired_by']]['slug'] for m in models.values()
                    if m.get('retired_by') and models[m['retired_by']]['slug'] in slugs})
    # Slugs this bot persisted that have no catalog history entry of their own.
    for row in data['surfaces']['claude-bot']:
        retired.update({old: models[row['key']]['slug'] for old in row.get('saved_aliases', [])})
    return {'default': models[data['defaults']['claude-bot']]['slug'],
            'choices': [{'name': discord_label(models[k]['label']), 'value': models[k]['slug']}
                        for k in keys],
            'retired': retired}


def gem_models() -> dict:
    """API and Antigravity pickers for the Gemini Discord bot.

    agy selects a tier by suffixing the base id (`gemini-3.8-flash-medium`), and
    its base id can differ from the API slug, hence `agy_id`. The first
    variant of the first gem-agy row is the default.
    """
    data = load()
    models, surfaces = data['models'], data['surfaces']
    name = lambda row: discord_label(row.get('label') or models[row['key']]['label'])
    api = [{'name': name(row), 'value': models[row['key']]['slug']} for row in surfaces['gem-api']]
    agy = []
    for row in surfaces['gem-agy']:
        base = models[row['key']].get('agy_id', models[row['key']]['slug'])
        for tier in row.get('variants') or [None]:
            agy.append({'name': name(row) + (f' ({tier.title()})' if tier else ''),
                        'value': base + (f'-{tier}' if tier else '')})
    return {'default_api': models[data['defaults']['gem-api']]['slug'],
            'default_agy': agy[0]['value'],
            'api_choices': api,
            'capabilities': {models[r['key']]['slug']: models[r['key']].get('capabilities', {}) for r in surfaces['gem-api']},
            'agy_choices': agy,
            'labels': {choice['value']: choice['name'] for choice in api},
            'cache_min_tokens': {models[row['key']]['slug']: models[row['key']]['cache_min_tokens']
                                 for row in surfaces['gem-api'] if models[row['key']].get('cache_min_tokens')},
            'default_summarization': models[data['defaults']['summarization']]['slug'],
            'pin_migrations': {
                'sonnet': f"{models['claude-sonnet']['slug']}-medium",
                'opus': f"{models['claude-opus']['slug']}-medium",
                'gemini_pro_prefix': models['agy-argon']['slug'],
            }}


def effort_map() -> dict[str, list[str]]:
    result = {}
    models = load()['models']
    for m in models.values():
        current = models.get(m.get('retired_by'), m)
        result[m['slug']] = list(current['efforts'])
    # Historical pricing slugs may share a name with a live CLI alias.
    # Resolve aliases last so those ledger entries cannot hide live controls.
    for m in models.values():
        if m.get('alias') and not m.get('retired_by'):
            result[m['alias']] = list(m['efforts'])
    return result


def effort_error(model: str, effort: str) -> str | None:
    """Why `effort` cannot run on catalog model slug `model`, or None.

    Only exact catalog slugs are checked; a retired slug uses its successor's
    ladder, as effort_map does. Aliases, fixed-tier variant names and models
    the catalog does not know pass through for their runtime to decide, so
    saved selections that predate the catalog keep working (audit F12)."""
    model, effort = (model or '').strip(), (effort or '').strip()
    if not model or not effort:
        return None
    models = load()['models']
    entry = next((m for m in models.values() if m['slug'] == model), None)
    if entry is None:
        return None
    ladder = list(models.get(entry.get('retired_by'), entry)['efforts'])
    if not ladder or effort in ladder:
        return None
    choices = ', '.join(ladder[:-1]) + ' or ' + ladder[-1] if len(ladder) > 1 else ladder[0]
    return f"effort '{effort}' is not available for {model}; choose {choices}"


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
            'cost_out': data['models'][entry['key']]['rates']['output'],
            **({'cost_after': {'at': data['models'][entry['key']]['rates_after']['at'],
               'cost_in': data['models'][entry['key']]['rates_after']['input'],
               'cost_out': data['models'][entry['key']]['rates_after']['output']}}
               if data['models'][entry['key']].get('rates_after') else {})}
            for entry in data['surfaces']['ticker']}


def fragwire_models() -> dict:
    """Defaults, chat picker, CLI invocation targets and pricing for Fragwire."""
    data = load()
    models, defaults = data['models'], data['defaults']
    claude = models[defaults['fragwire-claude']]
    agy = models[defaults['fragwire-agy']]
    gemini = models[defaults['fragwire-gemini']]
    codex = models[defaults['fragwire-codex']]
    ollama = models[defaults['fragwire-ollama']]
    flash_next = models[defaults['fragwire-flash-next']]
    chat_models = []
    cli_specs = {}
    ollama_label = ollama['label'].upper()
    for row in data['surfaces']['fragwire']:
        m = models[row['key']]
        key = row.get('name', row['key'])
        label = row.get('label', m['label'])
        if row['key'] == defaults['fragwire-ollama']:
            ollama_label = label.upper()
        item = {'key': key, 'label': label, 'group': row['group']}
        if 'fixed_effort' in row:
            item['fixed_effort'] = row['fixed_effort']
        else:
            item['efforts'] = list(m['efforts'])
            item['default_effort'] = row.get('default_effort', m['default_effort'])
        chat_models.append(item)
        runtime = row.get('runtime', m['transport'])
        if runtime in ('agy', 'claude', 'codex'):
            if runtime == 'agy' and m['transport'] != 'agy' and 'fixed_effort' in row:
                cli_model = f"{m['slug']}-{row['fixed_effort']}"
            elif row.get('value') == 'alias' or m.get('runtime_alias'):
                cli_model = m.get('alias', m['slug'])
            else:
                cli_model = m['slug']
            cli_specs[key] = {
                'runtime': runtime,
                'model': cli_model,
                'default_effort': '' if 'fixed_effort' in row else item['default_effort'],
            }
    flash_rates = models['agy-flash']['rates']
    argon = models['agy-argon']
    prices = {
        'gemini': (flash_rates['input'], flash_rates['output']),
        'gemini-argon-intro': (argon['rates']['input'], argon['rates']['output']),
        'gemini-argon': (argon['rates_after']['input'], argon['rates_after']['output']),
    }
    price_schedules = {
        'gemini': ({
            'match': 'argon',
            'slug': argon['slug'],
            'at': argon['rates_after']['at'],
            'before': 'gemini-argon-intro',
            'after': 'gemini-argon',
        },),
    }
    gemini_generation_configs = {}
    for row in data['surfaces']['gem-api']:
        m = models[row['key']]
        caps = m.get('capabilities') or {}
        if caps.get('max_output_tokens') and m.get('default_effort') in (caps.get('thinking_levels') or ()):
            gemini_generation_configs[m['slug']] = {
                'maxOutputTokens': caps['max_output_tokens'],
                'thinkingConfig': {'thinkingLevel': m['default_effort']},
            }
    family_models = []
    family_profiles = {}
    family_fallback_key = 'family-flash'
    family_fallback_label = argon['label']
    for row in data['surfaces']['fragwire-family']:
        m = models[row['key']]
        key = row['name']
        label = row.get('label') or m['label'].removeprefix('Claude ').removeprefix('Gemini ')
        effort = row['fixed_effort']
        family_models.append({
            'key': key,
            'label': label,
            'group': row['group'],
            'fixed_effort': effort,
        })
        runtime_id = m['slug'] if m['transport'] == 'agy' else f"{m['slug']}-{effort}"
        family_profiles[key] = (runtime_id, effort)
        if row.get('fallback'):
            family_fallback_key = key
            family_fallback_label = m['label']
    return {
        'defaults': {
            'claude_model': claude.get('alias', claude['slug']),
            'claude_effort': '',
            'agy_model': agy['slug'],
            'agy_effort': agy['efforts'][-1],
            'gemini_model': gemini['slug'],
            'codex_model': codex['slug'],
            'codex_effort': codex['default_effort'],
            'ollama_model': ollama['slug'],
            'ollama_label': ollama_label,
            'flash_next_model': flash_next['slug'],
        },
        'chat_models': chat_models,
        'cli_specs': cli_specs,
        'analysis_completion_telemetry_models': tuple(
            k for k, spec in cli_specs.items() if spec['runtime'] == 'codex'
        ),
        'prices': prices,
        'price_schedules': price_schedules,
        'gemini_generation_configs': gemini_generation_configs,
        'family_models': family_models,
        'family_profiles': family_profiles,
        'family_fallback_key': family_fallback_key,
        'family_fallback_label': family_fallback_label,
    }
