"""Run-completion pings — the cockpit's notification surface.

A finished run used to be invisible unless you were looking at the cockpit
(the owner 2026-08-05: "Polished for 7m 13s" is a thing you found out by looking).
These tests pin the two things that matter: the message says what happened
without being opened, and a ping failure can never touch the run.
"""
import os
import time
import types

import pytest

import operator_ping as OP


def _facts(**over) -> dict:
    f = {
        "state": "done", "reason": "exit 0",
        "task": "book AC 8807 SEA to YVR for Aug 6",
        "bot": "claude-d", "model": "claude-opus-5", "effort": "high",
        "surface": "browser", "runtime": "claude",
        "duration_s": 433.0, "n_messages": 41, "tokens": 128_400,
        "demo": False,
    }
    f.update(over)
    return f


def _runner(**over):
    r = types.SimpleNamespace(
        bot="claude-d", task="book a flight", state="done",
        model="claude-opus-5", effort="high", surface="browser",
        demo=False, started_ts=time.time() - 42.0, ended_ts=time.time(),
        _runtime="claude", _cum_in_tokens=1234, _peak_in_tokens=999,
        messages=[{"role": "assistant", "text": "done"}],
    )
    for k, v in over.items():
        setattr(r, k, v)
    return r


def _head(out: str) -> str:
    """The status row. It sits inside the fence (2026-08-11), so it is the line
    after the opening ```diff rather than the first line of the message."""
    return out.splitlines()[1]


# ── the message itself ──────────────────────────────────────────────────

def test_ping_leads_with_the_outcome_and_carries_the_task():
    out = OP.format_ping(_facts())
    assert OP.GLYPHS["done"] in _head(out)
    assert "book AC 8807 SEA to YVR" in out


def test_a_stopped_run_reads_differently_from_a_clean_finish():
    done = OP.format_ping(_facts(state="done"))
    stopped = OP.format_ping(_facts(state="interrupted", reason="user stop"))
    assert _head(done) != _head(stopped)
    assert "user stop" in stopped


def test_an_errored_run_names_its_reason():
    out = OP.format_ping(_facts(state="error", reason="no progress for 4m"))
    assert "no progress for 4m" in out


def test_duration_reads_exactly_like_the_agent_view():
    """Two live readouts in one channel must not format time two ways."""
    assert OP._fmt_duration(433) == "7m13s"
    assert OP._fmt_duration(9) == "9s"
    assert OP._fmt_duration(3725) == "1h02m"
    assert OP._fmt_duration(120) == "2m"


def test_a_long_task_is_truncated_not_dropped():
    out = OP.format_ping(_facts(task="find me " + "a very long errand " * 40))
    assert "find me a very long errand" in out
    assert "…" in out


def test_the_message_stays_inside_discords_limit():
    out = OP.format_ping(_facts(task="x" * 5000, reason="y" * 5000))
    assert len(out) <= OP.DISCORD_LIMIT


def test_token_spend_is_human_readable():
    assert "128.4k" in OP.format_ping(_facts(tokens=128_400))
    assert "1.2M" in OP.format_ping(_facts(tokens=1_234_000))


# ── who gets pinged ─────────────────────────────────────────────────────

def test_a_demo_run_never_pings():
    """Demo/prod isolation is a safety property (1.0.16) — a public visitor's
    run must not buzz the owner's phone."""
    assert OP.should_ping(_facts(demo=True)) is False
    assert OP.should_ping(_facts(demo=False)) is True


# ── the final frame ─────────────────────────────────────────────────────

def test_latest_shot_picks_the_newest_frame_from_this_run(tmp_path):
    old, mid, new = (tmp_path / n for n in ("a.png", "b.png", "c.png"))
    for p in (old, mid, new):
        p.write_bytes(b"x")
    os.utime(old, (100, 100))
    os.utime(mid, (200, 200))
    os.utime(new, (300, 300))
    assert OP.latest_shot(150, 400, [str(tmp_path)]) == str(mid.parent / "c.png")


def test_latest_shot_ignores_frames_from_before_the_run(tmp_path):
    stale = tmp_path / "stale.png"
    stale.write_bytes(b"x")
    os.utime(stale, (100, 100))
    assert OP.latest_shot(200, 300, [str(tmp_path)]) is None


def test_latest_shot_ignores_non_images(tmp_path):
    log = tmp_path / "run.log"
    log.write_bytes(b"x")
    os.utime(log, (250, 250))
    assert OP.latest_shot(200, 300, [str(tmp_path)]) is None


def test_latest_shot_survives_a_missing_dir():
    assert OP.latest_shot(0, 1, ["/nope/not/here"]) is None


# ── the contract with the run ───────────────────────────────────────────

def test_no_channel_configured_means_no_ping(monkeypatch):
    monkeypatch.delenv(OP.CHANNEL_ENV, raising=False)
    sent = []
    monkeypatch.setattr(OP, "_post", lambda *a, **k: sent.append(a) or (True, "", "9"))
    assert OP.notify(_runner()) is False
    assert sent == []


def test_a_send_failure_never_reaches_the_run(monkeypatch):
    """Same contract as the flight recorder: notification is best-effort, a
    run must never die because Discord did."""
    monkeypatch.setenv(OP.CHANNEL_ENV, "123")
    monkeypatch.setattr(OP, "_token", lambda: "t")

    def boom(*a, **k):
        raise RuntimeError("discord is down")

    monkeypatch.setattr(OP, "_post", boom)
    assert OP.notify(_runner()) is False   # returns, does not raise


def test_a_finished_run_posts_once(monkeypatch):
    monkeypatch.setenv(OP.CHANNEL_ENV, "123")
    monkeypatch.setattr(OP, "_token", lambda: "t")
    calls = []
    monkeypatch.setattr(OP, "_post",
                        lambda ch, text, img: calls.append((ch, text, img)) or (True, "", "9"))
    assert OP.notify(_runner(), reason="exit 0") is True
    assert len(calls) == 1
    channel, text, _img = calls[0]
    assert channel == "123"
    assert "book a flight" in text


def test_a_run_with_no_token_does_not_post(monkeypatch):
    monkeypatch.setenv(OP.CHANNEL_ENV, "123")
    monkeypatch.setattr(OP, "_token", lambda: None)
    monkeypatch.setattr(OP, "_post", lambda *a, **k: pytest.fail("posted with no token"))
    assert OP.notify(_runner()) is False


# ── the readout is monospaced ───────────────────────────────────────────
# 2026-08-05 kept the heading in markdown above the block. 2026-08-11 pulled it
# inside: a bold line floating over a fence reads as two messages, and on a
# phone it wrapped away from the block it belongs to.

def test_the_whole_ping_is_one_block_with_nothing_loose_above_it():
    out = OP.format_ping(_facts())
    assert out.startswith("```diff") and out.rstrip().endswith("```")
    assert "**" not in out            # no markdown — none of it renders in a fence
    assert "Finished" in _head(out)
    assert "book AC 8807" in out


def test_the_outcome_is_carried_by_the_diff_marker():
    """Inside a fence there is no bold, so green/red is the only signal left
    for whether the run worked."""
    assert _head(OP.format_ping(_facts(state="done"))).startswith("+")
    assert _head(OP.format_ping(_facts(state="error"))).startswith("-")
    assert _head(OP.format_live(_facts(state="running"), [])).startswith(" ")


def test_a_task_containing_a_fence_cannot_break_out_of_the_block():
    """An unbalanced fence would spill the rest of the message into the
    channel as prose — the runaway-fence failure."""
    out = OP.format_ping(_facts(task="run ```rm -rf``` for me"))
    assert out.count("```") == 2


# ── the in-flight card ──────────────────────────────────────────────────

def test_the_live_card_shows_only_what_it_is_doing_now():
    """Printing every completed action grew a wall of Clicking/Took screenshot
    rows that pushed the card past the fence width and buried the one thing you
    want at a glance (the owner 2026-08-11). The history lives in the cockpit."""
    steps = [("Navigating", "aircanada.com", 100.0), ("Typing", "SEA", 104.0),
             ("Clicking", "(420, 315)", 110.0)]
    out = OP.format_live(_facts(state="running", ended_ts=115.0), steps)
    assert "Clicking" in out and "(420, 315)" in out
    assert "Navigating" not in out and "Typing" not in out
    assert "Running" in _head(out)
    row = [ln for ln in out.splitlines() if "Clicking" in ln][0]
    assert OP._STEP_LIVE in row          # it pulses while the run is live
    assert "5s" in row                   # and says how long it has been on it


def test_every_row_stays_inside_the_card_width():
    """Same clamp the claude bot's tool trace uses. A line past the fence wraps,
    and a wrapped line loses its diff marker and renders as its own unstyled
    chunk — so overflow breaks the colouring, not just the tidiness."""
    steps = [("Clicking", "y" * 300, 100.0)]
    out = OP.format_live(_facts(state="running", task="x" * 400,
                                ended_ts=200.0), steps)
    body = out.split("\n")[1:-1]                 # drop the fences
    assert body, "card rendered empty"
    assert max(len(ln) for ln in body) <= OP.LINE_MAX
    assert OP.LINE_MAX == 88                     # the card width, not a new number


def test_the_card_is_a_fixed_size_however_chatty_the_run():
    """A 60-step run and a 1-step run produce the same card, because only the
    current action is on it."""
    one = OP.format_live(_facts(state="running", ended_ts=200.0),
                         [("Step59", "x", 199.0)])
    many = OP.format_live(_facts(state="running", ended_ts=200.0),
                          [(f"Step{i}", "x", 100.0 + i) for i in range(60)])
    assert len(one.splitlines()) == len(many.splitlines())
    assert "Step0 " not in many and "Step59" in many


def test_a_short_trace_is_not_padded_or_truncated():
    steps = [("Navigating", "example.com", 100.0)]
    out = OP.format_live(_facts(state="running", ended_ts=101.0), steps)
    assert "earlier" not in out
    assert "Navigating" in out


def test_a_run_that_has_done_nothing_yet_says_so():
    assert "(starting)" in OP.format_live(_facts(state="running"), [])


def test_the_headline_names_the_driver():
    """Four bots share the cockpit — whose run this is comes before anything
    else about it (the owner 2026-08-06)."""
    for bot in ("claude-a", "claude-b", "gpt", "gemma"):
        head = _head(OP.format_live(_facts(state="running", bot=bot), []))
        assert bot in head
    assert "gpt" in _head(OP.format_ping(_facts(bot="gpt")))


def test_the_running_card_spins_and_a_finished_one_does_not():
    frames = {_head(OP.format_live(_facts(state="running"), [], i)).split()[0]
              for i in range(len(OP._SPINNER))}
    assert frames == set(OP._SPINNER)
    # a retired card gets its outcome mark back, not a stopped wheel
    assert OP.GLYPHS["done"] in _head(OP.format_live(_facts(state="done"), [], 3))
    assert OP.GLYPHS["done"] == "●"       # the agent view's settled dot


def test_the_spinner_is_not_a_content_change():
    """Otherwise the change-detector would fire on every poll and the edit
    throttle would mean nothing."""
    a = OP.live_body(_facts(state="running", ended_ts=100.0), [("Clicking", "x", 90.0)])
    b = OP.live_body(_facts(state="running", ended_ts=100.0), [("Clicking", "x", 90.0)])
    assert a == b


def test_a_quiet_run_still_ticks_the_wheel(monkeypatch):
    clock, edits = _Clock(), []
    r = _runner(state="running", messages=[])
    _live_env(monkeypatch, r, edits)
    polls = {"n": 0}

    def sleep(s):
        clock.sleep(s)
        polls["n"] += 1
        if polls["n"] > 20:
            r.state = "done"

    OP.watch(r, now=clock.now, sleep=sleep)
    # 20 polls x 3s = 60s of silence: a few heartbeat ticks, not one per poll
    assert 3 <= len(edits) <= 6
    spun = {_head(e).split()[0] for e in edits if "Running" in e}
    assert len(spun) > 1, "the wheel never moved"


def test_a_gemma_run_says_why_it_has_no_trace():
    """agy returns plain text with no event stream, so there is no live trace
    to show. An empty card would read as a wedged run."""
    out = OP.format_live(_facts(state="running", runtime="agy"), [])
    assert "no live trace" in out
    assert "(starting)" not in out


def test_the_card_stops_claiming_to_be_running_once_it_is_not():
    """The watcher's last edit retires the card. A dashboard still showing
    Running an hour after the run ended is worse than no dashboard."""
    out = OP.format_live(_facts(state="done"), [("Clicking", "Submit", 100.0)])
    assert "Finished" in _head(out)
    assert "Running" not in out


def test_live_steps_reads_actions_and_errors_off_the_runner():
    r = _runner(messages=[
        {"role": "action", "text": "Navigating", "detail": "aircanada.com"},
        {"role": "assistant", "text": "thinking out loud"},
        {"role": "error", "text": "Action failed"},
    ])
    assert OP.live_steps(r) == [("Navigating", "aircanada.com", 0.0),
                                ("⚠", "Action failed", 0.0)]


class _Clock:
    """Monotonic-ish fake: every sleep advances it, so the edit floor is real
    without the test taking real seconds."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _live_env(monkeypatch, runner, edits):
    monkeypatch.setenv(OP.CHANNEL_ENV, "123")
    monkeypatch.setattr(OP, "_token", lambda: "t")
    monkeypatch.setattr(OP, "_post", lambda ch, text, img: (True, "", "77"))
    monkeypatch.setattr(OP, "_edit",
                        lambda ch, mid, text: edits.append(text) or (True, "", mid))


def test_the_card_follows_the_newest_action(monkeypatch):
    clock, edits = _Clock(), []
    r = _runner(state="running", messages=[])
    _live_env(monkeypatch, r, edits)

    def sleep(s):
        clock.sleep(s)
        n = len(r.messages)
        if n < 3:
            r.messages.append({"role": "action", "text": f"Step{n}", "detail": ""})
        else:
            r.state = "done"

    assert OP.watch(r, now=clock.now, sleep=sleep) == "77"
    assert edits, "the card never updated"
    # the card FOLLOWS the newest action rather than accumulating them
    assert "Step2" in edits[-1] and "Step0" not in edits[-1]


def test_a_quiet_run_does_not_burn_an_edit_per_poll(monkeypatch):
    """Discord rate-limits PATCH. A silent agent costs the spinner's heartbeat
    (LIVE_SPIN_S) and nothing more — never one edit per poll. The heartbeat is
    a deliberate cost added on 2026-08-06: before the spinner, a silent run
    cost exactly one edit, the retire."""
    clock, edits = _Clock(), []
    r = _runner(state="running", messages=[])
    _live_env(monkeypatch, r, edits)
    polls = {"n": 0}

    def sleep(s):
        clock.sleep(s)
        polls["n"] += 1
        if polls["n"] > 8:
            r.state = "done"

    OP.watch(r, now=clock.now, sleep=sleep)
    assert len(edits) <= 3                  # 8 polls (24s) → 1 tick + the retire
    assert "Finished" in _head(edits[-1])


def test_the_watcher_lets_go_when_the_run_ends(monkeypatch):
    clock, edits = _Clock(), []
    r = _runner(state="done", messages=[])
    _live_env(monkeypatch, r, edits)
    assert OP.watch(r, now=clock.now, sleep=clock.sleep) == "77"   # returns, no hang


def test_no_channel_means_no_live_card(monkeypatch):
    monkeypatch.delenv(OP.CHANNEL_ENV, raising=False)
    monkeypatch.setattr(OP, "_post", lambda *a, **k: pytest.fail("posted with no channel"))
    assert OP.watch(_runner(state="running")) is None


def test_a_demo_run_gets_no_live_card(monkeypatch):
    monkeypatch.setenv(OP.CHANNEL_ENV, "123")
    monkeypatch.setattr(OP, "_token", lambda: "t")
    monkeypatch.setattr(OP, "_post", lambda *a, **k: pytest.fail("demo posted"))
    assert OP.watch(_runner(state="running", demo=True)) is None


def test_an_error_row_is_marked_once_not_twice():
    """`●  ⚠  Action failed` marks the same row twice and reads as two events."""
    out = OP.format_live(_facts(state="running", ended_ts=110.0),
                         [("Navigating", "x", 100.0), ("⚠", "Action failed", 105.0)])
    err = [ln for ln in out.splitlines() if "Action failed" in ln][0]
    assert err.startswith("- ⚠")           # red, and marked exactly once
    assert OP._STEP_DONE not in err
