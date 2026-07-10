"""Operator background housekeeping: scheduled saved-tasks (#2) + run-completion
pings (#3).

One thread, two small jobs that share the same poll loop:
- SCHEDULE: saved tasks may carry a 5-field cron `schedule`; when a minute
  matches, the task is dispatched through the same path as the ▶ run route.
  Fired at most once per matching minute; the fired-keys are persisted to disk
  (operator-fired.json, atomic tmp+replace) so a server restart inside the same
  minute does NOT re-fire — each fire is a real token + browser cost.
- UNSEEN COUNTER: when a run reaches a terminal state (done/error — a user
  interrupt is the user's own act), bump a small on-disk counter that feeds
  the red notification badge on the host-app operator nav tab. The badge
  clears when the cockpit is actually looked at (the operator page view and
  its status poll both clear it), so an open cockpit never accumulates.
  (Replaces the short-lived Discord completion pings — "we don't need to be
  notified in discord for operator", 2026-07-01.)

Config (env): OPERATOR_SCHEDULER=0 disables the whole thread.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

import operator_tasks as _tasks_store

log = logging.getLogger("operator.schedule")



# ── 5-field cron matcher (stdlib-only; a bad expr never fires) ───────────────

def _parse_field(spec: str, lo: int, hi: int) -> tuple[set, bool] | None:
    """One cron field → (matching ints, is-wildcard), or None if invalid.
    Wildcard = the field starts with '*' (vixie-style, so */n counts too);
    cron_matches needs this to apply the standard dom/dow OR rule."""
    spec = spec.strip()
    wild = spec.startswith("*")
    vals: set = set()
    for part in spec.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, _, s = part.partition("/")
            if not s.isdigit() or int(s) < 1:
                return None
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, _, b_s = part.partition("-")
            if not (a_s.isdigit() and b_s.isdigit()):
                return None
            a, b = int(a_s), int(b_s)
        elif part.isdigit():
            a = b = int(part)
        else:
            return None
        if a < lo or b > hi or a > b:
            return None
        vals.update(range(a, b + 1, step))
    return vals, wild


def cron_valid(expr: str) -> bool:
    """Parse-only check for a schedule string; empty = 'no schedule' = valid."""
    expr = (expr or "").strip()
    if not expr:
        return True
    parts = expr.split()
    if len(parts) != 5:
        return False
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    return all(_parse_field(spec, lo, hi) is not None
               for spec, (lo, hi) in zip(parts, ranges))


def cron_matches(expr: str, dt: datetime) -> bool:
    """True iff a 5-field cron expression (min hour dom mon dow) matches dt.
    dow: 0=Sunday, 7 also accepted as Sunday. Invalid expressions are False."""
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    sets, wild = [], []
    for spec, (lo, hi) in zip(parts, ranges):
        parsed = _parse_field(spec, lo, hi)
        if parsed is None:
            return False
        sets.append(parsed[0])
        wild.append(parsed[1])
    dow = dt.isoweekday() % 7          # python Mon=1..Sun=7 → cron Sun=0
    dom_ok = dt.day in sets[2]
    dow_ok = dow in sets[4] or (dow == 0 and 7 in sets[4])
    # standard (vixie) cron: when BOTH dom and dow are restricted, the day
    # matches when EITHER does — `0 9 1 * 1` fires on the 1st OR any Monday.
    # ANDing them (the old behavior) silently missed almost every run.
    if not wild[2] and not wild[4]:
        day_ok = dom_ok or dow_ok
    else:
        day_ok = dom_ok and dow_ok
    return (dt.minute in sets[0] and dt.hour in sets[1]
            and dt.month in sets[3] and day_ok)


# ── schedule core (injected task loader, disk-persisted per-minute dedupe) ────

# Sibling of operator-tasks.json / operator-unseen.json; same cache dir + the
# tmp+os.replace atomic-write discipline. Persisting the fired-keys means a
# service restart INSIDE a matching minute won't re-fire tasks that already ran
# this minute (each fire is a real token + browser cost, so a double-fire is not
# free). The set is pruned to the current + previous minute so it stays tiny.
FIRED_PATH = os.path.join(
    os.path.expanduser("~/.cache/computer-use"), "operator-fired.json")


def _read_fired(path: str) -> dict:
    """The persisted {slug: 'YYYY-mm-ddTHH:MM'} fired-map. Missing/corrupt → {}."""
    import json
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_fired(path: str, d: dict) -> None:
    """Atomically persist the fired-map (tmp + os.replace); best-effort."""
    import json
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except OSError:
        pass


class SchedulerCore:
    def __init__(self, load_tasks=None, fired_path: str | None = None):
        self._load = load_tasks or _tasks_store.load_tasks
        self._fired_path = fired_path or FIRED_PATH
        # slug -> "YYYY-mm-ddTHH:MM" last fired; loaded from disk so a restart
        # inside the same minute doesn't re-fire.
        self._fired: dict = _read_fired(self._fired_path)

    def due(self, now: datetime) -> list:
        """Slugs whose schedule matches `now`'s minute and haven't fired in it.
        A fire is persisted immediately so a restart this minute won't repeat it."""
        key = now.strftime("%Y-%m-%dT%H:%M")
        out = []
        try:
            tasks = self._load() or {}
        except Exception:
            return []
        for slug, t in tasks.items():
            expr = (t.get("schedule") or "").strip()
            if not expr:
                continue
            if self._fired.get(slug) == key:
                continue
            if cron_matches(expr, now):
                self._fired[slug] = key
                out.append(slug)
        if out:
            self._prune(now)
            _write_fired(self._fired_path, self._fired)
        return out

    def _prune(self, now: datetime) -> None:
        """Keep only current + previous minute buckets so the set stays bounded."""
        from datetime import timedelta
        keep = {now.strftime("%Y-%m-%dT%H:%M"),
                (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")}
        self._fired = {slug: k for slug, k in self._fired.items() if k in keep}


# ── unseen-runs counter (feeds the nav badge) ────────────────────────────────

UNSEEN_PATH = os.path.join(
    os.path.expanduser("~/.cache/computer-use"), "operator-unseen.json")


def _read_unseen() -> dict:
    try:
        with open(UNSEEN_PATH, encoding="utf-8") as f:
            d = __import__("json").load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_unseen(d: dict) -> None:
    import json
    try:
        os.makedirs(os.path.dirname(UNSEEN_PATH), exist_ok=True)
        tmp = UNSEEN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, UNSEEN_PATH)
    except OSError:
        pass


def bump_unseen(text: str) -> None:
    """One more finished-but-unlooked-at run; keeps the last few summaries."""
    d = _read_unseen()
    d["count"] = int(d.get("count") or 0) + 1
    items = d.get("items") or []
    items.append({"text": text, "ts": time.time()})
    d["items"] = items[-10:]
    _write_unseen(d)


def unseen_count() -> int:
    try:
        return int(_read_unseen().get("count") or 0)
    except (TypeError, ValueError):
        return 0


def clear_unseen() -> None:
    if unseen_count():
        _write_unseen({"count": 0, "items": []})


def _fmt_secs(s: float) -> str:
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    return f"{m}m {sec}s" if sec else f"{m}m"


class CompletionWatcher:
    """Polls the runner's state; on a running→terminal transition records one
    unseen-run notification. `mark_scheduled(slug)` tags the in-flight run as
    scheduler-launched — consumed by the next terminal transition."""

    def __init__(self, runner, notify):
        self._r = runner
        self._notify = notify
        self._prev = getattr(runner, "state", "idle")
        self._sched_slug: str | None = None

    def mark_scheduled(self, slug: str) -> None:
        self._sched_slug = slug

    def poll(self) -> None:
        state = getattr(self._r, "state", "idle")
        prev, self._prev = self._prev, state
        if state == prev or prev != "running":
            return
        if state not in ("done", "error"):      # interrupted = the user's own act
            self._sched_slug = None
            return
        slug, self._sched_slug = self._sched_slug, None
        try:
            dur = (self._r.ended_ts or time.time()) - (self._r.started_ts or 0)
        except Exception:
            dur = 0
        icon = "✅" if state == "done" else "⚠️"
        task = (getattr(self._r, "task", "") or "")[:80]
        bot = getattr(self._r, "bot", "") or "operator"
        via = f" (scheduled: {slug})" if slug else ""
        try:
            self._notify(f"{icon} {bot} {'finished' if state == 'done' else 'ERRORED'}"
                         f"{via}: {task} · {_fmt_secs(dur or 0)}")
        except Exception:
            pass


# ── the background thread (singleton) ────────────────────────────────────────

_started = False


def start(run_fn, runner) -> None:
    """Launch the housekeeping thread once. `run_fn(slug)` dispatches a saved
    task (the view's shared run path); `runner` is the AgentRunner singleton."""
    global _started
    if _started or os.environ.get("OPERATOR_SCHEDULER", "1") == "0":
        return
    _started = True
    core = SchedulerCore()
    watcher = CompletionWatcher(runner, bump_unseen)

    def _loop():
        while True:
            try:
                watcher.poll()
                # naive local now(): a DST spring-forward skips the 02:xx wall
                # minutes entirely, so a schedule inside that hour misses that
                # day once a year (a tz-aware scheduler is out of scope)
                for slug in core.due(datetime.now()):
                    log.info("schedule fires: %s", slug)
                    try:
                        r = run_fn(slug) or {}
                        if r.get("ok"):
                            watcher.mark_scheduled(slug)
                        else:
                            bump_unseen(f"⏭️ scheduled task “{slug}” "
                                        f"skipped: {r.get('error', 'dispatch failed')}")
                    except Exception as e:  # noqa: BLE001
                        log.warning("scheduled run %s failed: %s", slug, e)
            except Exception:  # noqa: BLE001 — the loop must survive anything
                pass
            time.sleep(5)

    threading.Thread(target=_loop, daemon=True, name="operator-schedule").start()
