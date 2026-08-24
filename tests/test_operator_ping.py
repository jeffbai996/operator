"""Human-handoff pings — the cockpit's notification surface.

These tests pin the two things that matter: ordinary Operator activity is
silent, and an explicit TAKE_CONTROL request produces one useful card without
letting a Discord failure touch the run.
"""
import os
import time
import types

import pytest

import operator_agent as OA
import operator_ping as OP


def _facts(**over) -> dict:
    f = {
        "state": "done", "reason": "exit 0",
        "task": "book AC 8807 SEA to YVR for Aug 6",
        "bot": "claude-d", "model": "claude-opus-5", "effort": "high",
        "surface": "browser", "runtime": "claude",
        "duration_s": 433.0, "n_messages": 41, "tokens": 128_400,
        "demo": False, "handoff": "approve the final purchase",
    }
    f.update(over)
    return f


def _runner(**over):
    r = types.SimpleNamespace(
        bot="claude-d", task="book a flight", state="done",
        model="claude-opus-5", effort="high", surface="browser",
        demo=False, started_ts=time.time() - 42.0, ended_ts=time.time(),
        _runtime="claude", _cumulative_in_tokens=1234, _peak_in_tokens=999,
        handoff={"reason": "approve the final purchase", "ts": time.time()},
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

def test_ping_leads_with_the_human_need_and_carries_the_task():
    out = OP.format_ping(_facts())
    assert OP.GLYPHS["handoff"] in _head(out)
    assert "Operator needs your input" in _head(out)
    assert "approve the final purchase" not in _head(out)
    assert out.count("approve the final purchase") == 1
    assert "book AC 8807 SEA to YVR" in out


def test_handoff_reason_outranks_the_terminal_process_state():
    done = OP.format_ping(_facts(state="done", reason="exit 0"))
    errored = OP.format_ping(_facts(state="error", reason="no progress for 4m"))
    assert _head(done) == _head(errored)
    assert "approve the final purchase" in done
    assert "no progress for 4m" not in errored


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


def test_an_ordinary_operator_event_never_pings():
    assert OP.should_ping(_facts(handoff="")) is False


def test_runner_terminal_transition_pages_only_for_handoff(monkeypatch):
    pings = []
    monkeypatch.setattr(OA.operator_history, "record", lambda *a, **k: None)
    monkeypatch.setattr(OA.operator_restart_guard, "consume_and_restart",
                        lambda: None)
    monkeypatch.setattr(OA.operator_ping, "notify_async",
                        lambda runner, reason="": pings.append(reason))
    runner = types.SimpleNamespace(
        state="running", handoff=None, last_progress_ts=0)
    OA.AgentRunner._set_state(runner, "done", "exit 0")
    assert pings == []

    runner.state = "running"
    runner.handoff = {"reason": "solve the captcha", "ts": time.time()}
    OA.AgentRunner._set_state(runner, "done", "exit 0")
    assert pings == ["exit 0"]


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


def test_a_human_handoff_posts_once(monkeypatch):
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
    assert "approve the final purchase" in text


def test_an_ordinary_completion_stays_silent(monkeypatch):
    monkeypatch.setenv(OP.CHANNEL_ENV, "123")
    monkeypatch.setattr(OP, "_token", lambda: "t")
    monkeypatch.setattr(OP, "_post",
                        lambda *a, **k: pytest.fail("ordinary completion posted"))
    assert OP.notify(_runner(handoff=None), reason="exit 0") is False


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
    assert "Operator needs your input" in _head(out)
    assert "book AC 8807" in out


def test_human_attention_is_carried_by_the_warning_marker():
    assert _head(OP.format_ping(_facts())).startswith("- ⚠")


def test_a_task_containing_a_fence_cannot_break_out_of_the_block():
    """An unbalanced fence would spill the rest of the message into the
    channel as prose — the runaway-fence failure."""
    out = OP.format_ping(_facts(task="run ```rm -rf``` for me"))
    assert out.count("```") == 2
