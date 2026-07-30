"""Operator saved-tasks store — named, re-runnable task bundles (#30).

A saved task is a stored dispatch: a prompt + preferred sites + default
bot/model/effort (+ optional start_url), re-runnable later without re-typing.
This is the "OpenAI-Operator-style reusable task" ask , v1:
the prompt+sites+model bundle, no scheduling and no hard tool sandbox (both
deferred to v2 — see the handoff spec).

One job per file: this module owns ONLY the persistence + slug logic for saved
tasks. The Flask routes (operator_view.py) call in here; the actual dispatch
(runner.start) stays in the view, exactly mirroring /operator/dispatch. Storage
follows the operator_agent atomic-write convention: a sibling JSON in the shared
computer-use cache dir, written tmp + os.replace so a crash mid-write can't
corrupt the store.
"""
from __future__ import annotations

import json
import os
import re
import time

# Sibling of operator-state.json, same cache dir + atomic-write discipline.
# OPERATOR_TASKS_PATH overrides the location — the public demo points this at
# a demo-scoped store so visitors never see (or touch) the owner's tasks.
TASKS_PATH = os.environ.get("OPERATOR_TASKS_PATH") or os.path.join(
    os.path.expanduser("~/.cache/computer-use"), "operator-tasks.json")


def _now_iso() -> str:
    """UTC ISO-8601 stamp (seconds), 'Z'-suffixed — matches the state-file style."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _slugify(name: str) -> str:
    """Kebab-case slug from a task name: lowercase, non-alnum → '-', trimmed.

    Empty/pure-symbol names fall back to 'task' so a slug is never blank (the
    caller still guards against empty NAMEs upstream; this is belt-and-suspenders).
    """
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "task"


def load_tasks() -> dict:
    """The full {slug: task} map. Missing/corrupt store → empty (never raises)."""
    try:
        with open(TASKS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        tasks = data.get("tasks", {})
        return tasks if isinstance(tasks, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_tasks(tasks: dict) -> None:
    """Atomically persist the {slug: task} map (tmp + os.replace)."""
    os.makedirs(os.path.dirname(TASKS_PATH), exist_ok=True)
    tmp = TASKS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, indent=2)
    os.replace(tmp, TASKS_PATH)


def _unique_slug(base: str, existing: dict, keep: str | None = None) -> str:
    """A slug not already used by a DIFFERENT task. `keep` is the slug we're
    updating in place (so re-saving a task under its own name doesn't -2 it).
    Collision → append -2, -3, … ."""
    if base not in existing or base == keep:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _clean_sites(sites) -> list[str]:
    """Normalize the preferred-sites field into a clean list of non-empty strings.
    Accepts a list or a comma-separated string (the UI sends a comma field)."""
    if isinstance(sites, str):
        parts = sites.split(",")
    elif isinstance(sites, list):
        parts = sites
    else:
        parts = []
    return [p.strip() for p in parts if isinstance(p, str) and p.strip()]


# ── {{variables}} (1.0.13) ───────────────────────────────────────────────────
# A saved prompt may carry {{name}} placeholders, filled at dispatch time.
# Names are word-ish ({{city}}, {{max price}}, {{ok_name-1}}); malformed or
# empty braces are left alone and never count as variables.
_VAR_RE = re.compile(r"\{\{([A-Za-z0-9_][A-Za-z0-9_ -]*)\}\}")


def extract_vars(prompt: str) -> list[str]:
    """Ordered unique {{variable}} names in a prompt ([] when none)."""
    seen: set = set()
    out: list[str] = []
    for m in _VAR_RE.finditer(prompt or ""):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def fill_vars(prompt: str, values: dict) -> tuple[str, list[str]]:
    """Substitute {{vars}} from `values` → (filled_text, missing_names).
    A blank/whitespace value counts as missing — a silent empty fill is never
    what the user meant. Unfilled placeholders are left intact so the caller
    can bounce the prompt back for completion."""
    values = values if isinstance(values, dict) else {}
    missing: list[str] = []

    def _sub(m):
        name = m.group(1).strip()
        v = str(values.get(name, "") or "").strip()
        if not v:
            if name not in missing:
                missing.append(name)
            return m.group(0)
        return v

    return _VAR_RE.sub(_sub, prompt or ""), missing


def save_task(fields: dict) -> tuple[str | None, str | None]:
    """Create or update a saved task from a dict of the data-model fields.

    Requires non-empty `name` and `prompt`. Slug = kebab(name); an existing slug
    for a DIFFERENT name collision gets -2. Re-saving under an existing exact slug
    (passed as `slug`) updates in place and preserves its `created` stamp.

    Returns (slug, None) on success or (None, error_message) on validation failure.
    """
    name = (fields.get("name") or "").strip()
    prompt = (fields.get("prompt") or "").strip()
    if not name:
        return None, "empty name"
    if not prompt:
        return None, "empty prompt"
    schedule = (fields.get("schedule") or "").strip()
    if schedule:
        # lazy import — operator_schedule imports this module at top level
        from operator_schedule import cron_valid
        if not cron_valid(schedule):
            return None, "schedule must be a 5-field cron (min hour dom mon dow)"
        if extract_vars(prompt):
            # nobody is there to fill values when cron fires — the combo can
            # only ever produce a 400 at dispatch time, so refuse it at save
            return None, ("scheduled tasks can't use {{variables}} — "
                          "there's nobody to fill them when the cron fires")

    tasks = load_tasks()
    # If an explicit slug was passed and exists, update it in place; else derive
    # a fresh unique slug from the name.
    explicit = (fields.get("slug") or "").strip()
    if explicit and explicit in tasks:
        slug = explicit
        created = tasks[slug].get("created") or _now_iso()
    else:
        slug = _unique_slug(_slugify(name), tasks)
        created = _now_iso()

    tasks[slug] = {
        "name": name,
        "prompt": prompt,
        "sites": _clean_sites(fields.get("sites")),
        "bot": (fields.get("bot") or "").strip(),
        "model": (fields.get("model") or "").strip(),
        "effort": (fields.get("effort") or "").strip(),
        "start_url": (fields.get("start_url") or "").strip(),
        "schedule": schedule,
        "created": created,
        "last_run": tasks.get(slug, {}).get("last_run"),
    }
    _save_tasks(tasks)
    return slug, None


def get_task(slug: str) -> dict | None:
    """One saved task by slug, or None."""
    return load_tasks().get(slug)


def delete_task(slug: str) -> bool:
    """Remove a saved task. True if it existed."""
    tasks = load_tasks()
    if slug not in tasks:
        return False
    del tasks[slug]
    _save_tasks(tasks)
    return True


def mark_run(slug: str) -> None:
    """Stamp last_run=now on a saved task (best-effort; silent if it's gone)."""
    tasks = load_tasks()
    if slug in tasks:
        tasks[slug]["last_run"] = _now_iso()
        _save_tasks(tasks)


def sites_preamble(sites: list[str]) -> str:
    """The v1 preferred-sites prompt hint (NOT a hard sandbox). Empty → ''."""
    clean = _clean_sites(sites)
    if not clean:
        return ""
    joined = ", ".join(clean)
    return (f"Prefer these sites for this task: {joined}. "
            f"If a step needs one of them, start there. ")
