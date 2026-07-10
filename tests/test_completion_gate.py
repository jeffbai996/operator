"""v1.1 §3.3 — completion gate + bounded auto-replan.

A clean exit that lacks recent visual evidence (or reads like a bail) gets ONE
follow-up resumed turn instead of `done`. Decision logic is unit-tested here;
the _run wiring is three lines riding the same _run_inner path every turn uses.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_completion_gate.py -q
"""
import pytest

import operator_agent as OA


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return OA.AgentRunner()


def _did_work(r, n=4):
    """Simulate n unverified desktop actions + a final assistant message."""
    for i in range(n):
        r._note_action("computer", {"action": "left_click", "coordinate": [i, i]})
    r.messages.append({"ts": 0, "role": "assistant", "text": "All done."})


# ── evidence ledger ──────────────────────────────────────────────────────────

def test_desktop_clicks_count_and_screenshot_resets(runner):
    runner._note_action("computer", {"action": "left_click", "coordinate": [1, 2]})
    runner._note_action("computer", {"action": "type", "text": "hi"})
    assert runner._consequential_acts == 2
    assert runner._acts_since_visual == 2
    runner._note_action("computer", {"action": "screenshot"})
    assert runner._acts_since_visual == 0
    assert runner._consequential_acts == 2   # looks aren't work


def test_playwright_actions_are_self_evidencing(runner):
    """browser_* results embed a page snapshot — consequential, but the visual
    counter resets, so browser-only runs don't trip the verify gate."""
    for _ in range(6):
        runner._note_action("mcp__playwright__browser_click", {"ref": "e1"})
    assert runner._consequential_acts == 6
    assert runner._acts_since_visual == 0


def test_perceive_is_a_look_not_work(runner):
    runner._note_action("computer", {"action": "left_click", "coordinate": [1, 2]})
    runner._note_action("perceive", {})
    assert runner._acts_since_visual == 0
    assert runner._consequential_acts == 1


# ── gate decision ────────────────────────────────────────────────────────────

def test_unverified_desktop_run_gets_the_verify_turn(runner):
    _did_work(runner)
    gate = runner._completion_gate_check()
    assert gate == OA.AgentRunner._GATE_VERIFY_PROMPT
    assert runner._gate_fired is True
    assert any("Completion check" in m["text"] for m in runner.messages
               if m["role"] == "error")


def test_run_ending_with_a_look_passes_clean(runner):
    _did_work(runner)
    runner._note_action("computer", {"action": "screenshot"})
    assert runner._completion_gate_check() == ""


def test_bail_message_gets_the_replan_turn_even_with_evidence(runner):
    _did_work(runner)
    runner._note_action("computer", {"action": "screenshot"})
    runner.messages.append({"ts": 0, "role": "assistant",
                            "text": "I was unable to find the export button."})
    gate = runner._completion_gate_check()
    assert gate == OA.AgentRunner._GATE_REPLAN_PROMPT
    assert any("Auto-replan" in m["text"] for m in runner.messages
               if m["role"] == "error")


def test_gate_fires_at_most_once_per_start(runner):
    _did_work(runner)
    assert runner._completion_gate_check() != ""
    assert runner._completion_gate_check() == ""   # second exit → accept done


def test_read_only_turn_never_gates(runner):
    runner.messages.append({"ts": 0, "role": "assistant", "text": "It's $42."})
    assert runner._completion_gate_check() == ""


def test_gate_env_kill_switch(runner, monkeypatch):
    monkeypatch.setenv("OPERATOR_COMPLETION_GATE", "0")
    _did_work(runner)
    assert runner._completion_gate_check() == ""


@pytest.mark.parametrize("attr,val", [
    ("demo", True),                       # public demo never burns extra turns
    ("_stopped", True),                   # user stop wins
    ("_tok_stop_fired", True),            # budget cap wins
    ("handoff", {"reason": "x", "ts": 0}),  # deliberate takeover ≠ bail
])
def test_gate_respects_run_overrides(runner, attr, val):
    _did_work(runner)
    setattr(runner, attr, val)
    assert runner._completion_gate_check() == ""


# ── the inter-turn gap ───────────────────────────────────────────────────────

def test_is_running_stays_true_across_the_gate_gap(runner):
    runner.state = "running"
    runner._proc = None
    assert runner.is_running() is False        # dead run reads dead (§2.2)
    runner._gate_pending = True
    assert runner.is_running() is True         # gate gap reads alive (§3.3)
