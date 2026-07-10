"""Tests for operator_schedule.py (#2 scheduled tasks + #3 completion pings).

Pure-logic tests: the cron matcher, per-minute dedupe, ping policy, and the
runner state-transition watcher — all with injected fakes, no threads/network.

Run with:  pytest test_operator_schedule.py -q
"""
from datetime import datetime

import operator_schedule as OS


def dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


# ── cron matcher ─────────────────────────────────────────────────────────────

def test_star_matches_any_minute():
    assert OS.cron_matches("* * * * *", dt("2026-07-02 06:15"))


def test_exact_minute_hour():
    assert OS.cron_matches("15 6 * * *", dt("2026-07-02 06:15"))
    assert not OS.cron_matches("15 6 * * *", dt("2026-07-02 06:16"))
    assert not OS.cron_matches("15 6 * * *", dt("2026-07-02 07:15"))


def test_weekday_range():
    # 2026-07-02 is a Thursday (cron dow: Sun=0, Thu=4)
    assert OS.cron_matches("15 6 * * 1-5", dt("2026-07-02 06:15"))
    # 2026-07-05 is a Sunday
    assert not OS.cron_matches("15 6 * * 1-5", dt("2026-07-05 06:15"))


def test_dow_sunday_as_0_and_7():
    assert OS.cron_matches("0 9 * * 0", dt("2026-07-05 09:00"))
    assert OS.cron_matches("0 9 * * 7", dt("2026-07-05 09:00"))


def test_step_and_list():
    assert OS.cron_matches("*/15 * * * *", dt("2026-07-02 06:30"))
    assert not OS.cron_matches("*/15 * * * *", dt("2026-07-02 06:31"))
    assert OS.cron_matches("0 9,17 * * *", dt("2026-07-02 17:00"))
    assert not OS.cron_matches("0 9,17 * * *", dt("2026-07-02 12:00"))


def test_dom_and_month():
    assert OS.cron_matches("0 0 1 7 *", dt("2026-07-01 00:00"))
    assert not OS.cron_matches("0 0 1 7 *", dt("2026-08-01 00:00"))


def test_dom_only_restricted_fires_any_weekday():
    # 2026-07-01 is a Wednesday; dow is '*' so only the day-of-month gates
    assert OS.cron_matches("0 9 1 * *", dt("2026-07-01 09:00"))
    assert not OS.cron_matches("0 9 1 * *", dt("2026-07-02 09:00"))


def test_dow_only_restricted_fires_any_dom():
    # 2026-07-06 is a Monday; dom is '*' so only the weekday gates
    assert OS.cron_matches("0 9 * * 1", dt("2026-07-06 09:00"))
    assert not OS.cron_matches("0 9 * * 1", dt("2026-07-07 09:00"))


def test_dom_and_dow_both_restricted_is_or():
    """1.0.8 B5: standard cron ORs dom/dow when BOTH are restricted —
    `0 9 1 * 1` fires on the 1st OR any Monday, not only a 1st that is
    a Monday. The old AND silently missed almost every intended run."""
    expr = "0 9 1 * 1"
    assert OS.cron_matches(expr, dt("2026-07-01 09:00"))      # the 1st (a Wednesday)
    assert OS.cron_matches(expr, dt("2026-07-06 09:00"))      # a Monday (the 6th)
    assert not OS.cron_matches(expr, dt("2026-07-10 09:00"))  # Friday the 10th
    assert OS.cron_matches(expr, dt("2026-06-01 09:00"))      # both: Mon June 1st


def test_neither_restricted_fires_daily():
    assert OS.cron_matches("0 9 * * *", dt("2026-07-10 09:00"))


def test_star_step_dom_counts_as_wildcard():
    # vixie-style: a field starting with '*' (incl. */n) is unrestricted for
    # the OR rule, so dom/dow stay ANDed here — Monday the 6th (even day)
    # fails the */2 dom, and odd days that aren't Monday fail the dow.
    expr = "0 9 */2 * 1"
    assert not OS.cron_matches(expr, dt("2026-07-06 09:00"))  # Mon, even day
    assert OS.cron_matches(expr, dt("2026-07-13 09:00"))      # Mon, odd day
    assert not OS.cron_matches(expr, dt("2026-07-03 09:00"))  # odd day, Friday


def test_invalid_exprs_never_fire():
    for bad in ("", "not cron", "61 * * * *", "* * * *", "a b c d e", "1-70 * * * *"):
        assert not OS.cron_matches(bad, dt("2026-07-02 06:15"))


# ── per-minute dedupe (tick) ─────────────────────────────────────────────────

def test_tick_fires_once_per_matching_minute():
    core = OS.SchedulerCore(load_tasks=lambda: {
        "morning": {"schedule": "15 6 * * *", "name": "Morning"},
        "nosched": {"name": "no schedule field"},
    })
    t = dt("2026-07-02 06:15")
    assert core.due(t) == ["morning"]
    assert core.due(t) == []                       # same minute → deduped
    assert core.due(dt("2026-07-03 06:15")) == ["morning"]   # next day fires again


# ── restart persistence (fired-keys survive a service restart) ───────────────

def _tasks():
    return lambda: {"morning": {"schedule": "15 6 * * *", "name": "Morning"}}


def test_fired_key_persists_across_restart_same_minute(tmp_path):
    # A service restart INSIDE the same matching minute must NOT re-fire the task.
    fired = str(tmp_path / "fired.json")
    t = dt("2026-07-02 06:15")

    core1 = OS.SchedulerCore(load_tasks=_tasks(), fired_path=fired)
    assert core1.due(t) == ["morning"]             # first instance fires

    # simulate a restart: brand-new SchedulerCore loading state from disk
    core2 = OS.SchedulerCore(load_tasks=_tasks(), fired_path=fired)
    assert core2.due(t) == []                      # already fired this minute → silent


def test_new_minute_fires_after_restart(tmp_path):
    # A genuinely new minute DOES fire even after a restart.
    fired = str(tmp_path / "fired.json")
    core1 = OS.SchedulerCore(load_tasks=_tasks(), fired_path=fired)
    assert core1.due(dt("2026-07-02 06:15")) == ["morning"]

    core2 = OS.SchedulerCore(load_tasks=_tasks(), fired_path=fired)
    # next day's 06:15 is a new bucket → fires
    assert core2.due(dt("2026-07-03 06:15")) == ["morning"]


def test_fired_set_pruned_to_recent_buckets(tmp_path):
    # The persisted fired-set must not grow unbounded: old buckets are pruned,
    # keeping only the current + previous minute. Distinct tasks that fire in
    # different minutes would otherwise accumulate forever.
    fired = str(tmp_path / "fired.json")
    many = lambda: {
        f"t{m}": {"schedule": f"{m} 6 * * *", "name": f"T{m}"}
        for m in (15, 16, 17, 18)
    }
    core = OS.SchedulerCore(load_tasks=many, fired_path=fired)
    core.due(dt("2026-07-02 06:15"))
    core.due(dt("2026-07-02 06:16"))
    core.due(dt("2026-07-02 06:17"))
    core.due(dt("2026-07-02 06:18"))

    persisted = OS._read_fired(fired)
    buckets = {v for v in persisted.values()}
    # only the last two minute-buckets remain (t17 @ 06:17, t18 @ 06:18)
    assert buckets == {"2026-07-02T06:17", "2026-07-02T06:18"}
    assert set(persisted.keys()) == {"t17", "t18"}


def test_due_survives_corrupt_fired_file(tmp_path):
    # A garbage or missing persistence file must never break firing.
    fired = str(tmp_path / "fired.json")
    with open(fired, "w") as f:
        f.write("{ not json")
    core = OS.SchedulerCore(load_tasks=_tasks(), fired_path=fired)
    assert core.due(dt("2026-07-02 06:15")) == ["morning"]


# ── unseen counter ───────────────────────────────────────────────────────────

def test_unseen_counter_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(OS, "UNSEEN_PATH", str(tmp_path / "unseen.json"))
    assert OS.unseen_count() == 0
    OS.bump_unseen("one"); OS.bump_unseen("two")
    assert OS.unseen_count() == 2
    OS.clear_unseen()
    assert OS.unseen_count() == 0


# ── completion watcher ───────────────────────────────────────────────────────

class FakeRunner:
    def __init__(self):
        self.state = "idle"
        self.bot = "claude-a"
        self.task = "check the thing on the site"
        self.started_ts = 1000.0
        self.ended_ts = 1130.0


def test_watcher_pings_on_done_transition():
    r = FakeRunner()
    pings = []
    w = OS.CompletionWatcher(r, notify=lambda txt: pings.append(txt))
    w.poll()                       # idle — no-op
    r.state = "running"
    w.poll()
    r.state = "done"
    w.poll()
    assert len(pings) == 1
    assert "claude-a" in pings[0] and "✅" in pings[0]
    w.poll()                       # still done — no duplicate
    assert len(pings) == 1


def test_watcher_counts_short_manual_run_too():
    # badge semantics: every terminal run counts (the open cockpit clears it)
    r = FakeRunner()
    r.ended_ts = 1030.0            # 30s run
    pings = []
    w = OS.CompletionWatcher(r, notify=lambda txt: pings.append(txt))
    r.state = "running"; w.poll()
    r.state = "done"; w.poll()
    assert len(pings) == 1


def test_watcher_always_pings_scheduled_runs_and_errors_flagged():
    r = FakeRunner()
    r.ended_ts = 1010.0            # short — but scheduled
    pings = []
    w = OS.CompletionWatcher(r, notify=lambda txt: pings.append(txt))
    r.state = "running"; w.poll()
    w.mark_scheduled("morning-semis-check")
    r.state = "error"; w.poll()
    assert len(pings) == 1
    assert "⚠️" in pings[0]


def test_watcher_ignores_interrupted():
    r = FakeRunner()
    pings = []
    w = OS.CompletionWatcher(r, notify=lambda txt: pings.append(txt))
    r.state = "running"; w.poll()
    r.state = "interrupted"; w.poll()
    assert pings == []


def test_cron_valid():
    assert OS.cron_valid("15 6 * * 1-5")
    assert OS.cron_valid("*/10 * * * *")
    assert not OS.cron_valid("15 6 * *")
    assert not OS.cron_valid("61 * * * *")
    assert not OS.cron_valid("gibberish")
    assert OS.cron_valid("")          # empty = "no schedule", allowed
