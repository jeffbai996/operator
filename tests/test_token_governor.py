"""Tests for the token governor (#34 phase A) — cumulative tracking + hard-cap
auto-stop in operator_agent._note_token_usage.

Why auto-stop and not warn-only: the 1.5M warn fired during the 89M-token
lichess run and protected nothing — nobody watches a headless trace. The cap
is the governor's teeth: it stops the run the way a human tap would.

Run with:  pytest test_token_governor.py -q
"""
import pytest

import operator_agent as OA


def make_runner(monkeypatch=None):
    r = OA.AgentRunner()
    # mirror the per-run resets _run() does before any token event can arrive
    r._peak_in_tokens = 0
    r._tok_warned = False
    r._cum_in_tokens = 0
    r._tok_stop_fired = False
    r.messages = []
    r.handoff = None
    return r


def stub_stop(r):
    """Replace the real stop() (kills process groups) with a recorder that
    mimics its one observable side effect the governor must survive:
    stop() clears handoff, so the governor has to set handoff AFTER stopping."""
    calls = []

    def _stop():
        calls.append(1)
        r.handoff = None   # real stop() does this — ordering regression guard
        return {"ok": True}

    r.stop = _stop
    return calls


def error_texts(r):
    return [m["text"] for m in r.messages if m.get("role") == "error"]


# ── accumulation ─────────────────────────────────────────────────────────────

def test_cumulative_accumulates_across_turns():
    r = make_runner()
    for it in (10_000, 25_000, 40_000):
        r._note_token_usage(it)
    assert r._cum_in_tokens == 75_000


def test_peak_tracks_max_single_turn_input():
    r = make_runner()
    for it in (10_000, 90_000, 40_000):
        r._note_token_usage(it)
    assert r._peak_in_tokens == 90_000


def test_garbage_usage_ignored():
    r = make_runner()
    for bad in (None, "abc", -5, 0, {}, 3.5):  # 3.5 is fine to int(); others no-op
        r._note_token_usage(bad)
    assert r._cum_in_tokens == 3          # int(3.5)
    assert r.messages == []


# ── hard cap: per-turn ───────────────────────────────────────────────────────

def test_turn_cap_trip_stops_run_once(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN_TURN_STOP", "100000")
    monkeypatch.setenv("OPERATOR_TOKEN_RUN_STOP", "0")
    r = make_runner()
    calls = stub_stop(r)
    r._note_token_usage(150_000)
    r._note_token_usage(200_000)   # second trip must NOT stop again
    assert calls == [1]
    caps = [t for t in error_texts(r) if "Token cap" in t]
    assert len(caps) == 1
    assert "150,000" in caps[0]


def test_handoff_set_after_stop_clears_it(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN_TURN_STOP", "100000")
    r = make_runner()
    stub_stop(r)
    r._note_token_usage(150_000)
    assert r.handoff is not None
    assert "token" in r.handoff["reason"].lower()


# ── hard cap: cumulative per-run ─────────────────────────────────────────────

def test_run_cap_trips_on_cumulative(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN_TURN_STOP", "0")
    monkeypatch.setenv("OPERATOR_TOKEN_RUN_STOP", "100000")
    r = make_runner()
    calls = stub_stop(r)
    r._note_token_usage(60_000)
    assert calls == []
    r._note_token_usage(60_000)    # cumulative 120k >= 100k cap
    assert calls == [1]
    assert any("cumulative" in t for t in error_texts(r))


def test_under_cap_no_stop_no_cap_message(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN_TURN_STOP", "100000")
    monkeypatch.setenv("OPERATOR_TOKEN_RUN_STOP", "100000")
    r = make_runner()
    calls = stub_stop(r)
    r._note_token_usage(30_000)
    assert calls == []
    assert error_texts(r) == []
    assert r.handoff is None


# ── config ───────────────────────────────────────────────────────────────────

def test_default_caps_when_env_unset(monkeypatch):
    monkeypatch.delenv("OPERATOR_TOKEN_TURN_STOP", raising=False)
    monkeypatch.delenv("OPERATOR_TOKEN_RUN_STOP", raising=False)
    assert OA._tok_caps() == (3_000_000, 20_000_000)


def test_env_zero_disables_auto_stop_warn_only(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN_TURN_STOP", "0")
    monkeypatch.setenv("OPERATOR_TOKEN_RUN_STOP", "0")
    r = make_runner()
    calls = stub_stop(r)
    r._note_token_usage(5_000_000)   # over the old warn threshold AND default caps
    assert calls == []
    warns = [t for t in error_texts(r) if "High token use" in t]
    assert len(warns) == 1           # legacy warn preserved
    assert not any("Token cap" in t for t in error_texts(r))


def test_env_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN_TURN_STOP", "not-a-number")
    assert OA._tok_caps()[0] == 3_000_000


# ── legacy warn regression ───────────────────────────────────────────────────

def test_warn_still_fires_once_at_threshold(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN_TURN_STOP", "0")
    monkeypatch.setenv("OPERATOR_TOKEN_RUN_STOP", "0")
    r = make_runner()
    r._note_token_usage(1_600_000)
    r._note_token_usage(1_700_000)
    warns = [t for t in error_texts(r) if "High token use" in t]
    assert len(warns) == 1
