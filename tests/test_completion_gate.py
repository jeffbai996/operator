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


def test_shell_and_file_tools_do_not_arm_visual_gate(runner):
    """Saving/inspecting an artifact is not an unverified screen action."""
    for name in ("run_command", "write_file", "edit_file"):
        runner._note_action(name, {"path": "/tmp/result.pdf"})
    assert runner._consequential_acts == 0
    assert runner._acts_since_visual == 0


def test_file_work_after_browser_evidence_finishes_without_regrounding(runner):
    """The live IRCC/PDF failure: a grounded browser result followed by shell
    commands must not summon a final screenshot and replace the useful answer.
    """
    runner._note_action("mcp__playwright__browser_click", {"ref": "download"})
    for i in range(4):
        runner._note_action("run_command", {"command": f"inspect-pdf {i}"})
    runner.messages.append({"ts": 0, "role": "assistant",
                            "text": "Saved CIT 0001 and CIT 0014."})
    assert runner._completion_gate_check() == ""


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
               if m["role"] == "notice")


def test_run_ending_with_a_look_passes_clean(runner):
    _did_work(runner)
    runner._note_action("computer", {"action": "screenshot"})
    assert runner._completion_gate_check() == ""


def test_bail_replan_is_opt_in_default_off(runner):
    """2026-07-28: second live false positive (claude-b runs) — bail-replan is
    now opt-in. A bail-sounding final with visual evidence passes clean unless
    OPERATOR_BAIL_REPLAN=1."""
    _did_work(runner)
    runner._note_action("computer", {"action": "screenshot"})
    runner.messages.append({"ts": 0, "role": "assistant",
                            "text": "I was unable to find the export button."})
    assert runner._completion_gate_check() == ""


def test_bail_message_gets_the_replan_turn_even_with_evidence(runner, monkeypatch):
    monkeypatch.setenv("OPERATOR_BAIL_REPLAN", "1")
    _did_work(runner)
    runner._note_action("computer", {"action": "screenshot"})
    runner.messages.append({"ts": 0, "role": "assistant",
                            "text": "I was unable to find the export button."})
    gate = runner._completion_gate_check()
    assert gate == OA.AgentRunner._GATE_REPLAN_PROMPT
    assert any("Auto-replan" in m["text"] for m in runner.messages
               if m["role"] == "notice")


@pytest.mark.parametrize("final", [
    # THE live false positive (2026-07-22): a finished passport-research answer
    # whose CONTENT contains "can't" — burned a full replan turn on a done task.
    "Validity: 5 years (under-16 passports can't be renewed, must reapply "
    "in person each time)",
    "Note: you cannot edit the DS-11 after printing, so double-check first.",
    "The fee can't be paid by card at USPS; bring a check. All set.",
    "You can take over the browser any time if you want to poke around.",
    "They were unable to confirm same-day pickup, so I chose Thursday.",
    "If it's not possible to attend, the ticket is refundable until Friday.",
    "I set it up so you won't have to stop by the branch.",
    "Done — I stopped the auto-renewal as requested.",
])
def test_negations_in_answer_content_do_not_trigger_replan(runner, final):
    """Bail markers must be FIRST-PERSON anchored — third-party or factual
    can't/cannot/unable in a successful summary is not a bail."""
    _did_work(runner)
    runner._note_action("computer", {"action": "screenshot"})
    runner.messages.append({"ts": 0, "role": "assistant", "text": final})
    assert runner._completion_gate_check() == ""


@pytest.mark.parametrize("final", [
    "I couldn't get past the login wall.",
    "I can't complete the checkout — the card field rejects input.",
    "I'm blocked by a captcha here.",
    "I am stuck on the 2FA screen.",
    "I'll stop here — the page keeps erroring.",
    "Failed to complete the booking, the slot vanished.",
    "Please take over for the payment step.",
    "You'll need to take over to enter the SMS code.",
])
def test_first_person_bails_still_trigger_replan(runner, final, monkeypatch):
    monkeypatch.setenv("OPERATOR_BAIL_REPLAN", "1")
    _did_work(runner)
    runner._note_action("computer", {"action": "screenshot"})
    runner.messages.append({"ts": 0, "role": "assistant", "text": final})
    assert runner._completion_gate_check() == OA.AgentRunner._GATE_REPLAN_PROMPT


def test_gate_fires_at_most_once_per_start(runner):
    _did_work(runner)
    assert runner._completion_gate_check() != ""
    assert runner._completion_gate_check() == ""   # second exit → accept done


def test_verify_followup_preserves_the_substantive_answer(runner):
    original = "Saved CIT 0001 and CIT 0014, with both file paths."
    runner.messages.append({"ts": 0, "role": "assistant", "text": original})
    runner._transcript.append({"role": "assistant", "text": original})
    runner._gate_verify_active = True
    runner._gate_original_final = original
    runner._gate_message_start = len(runner.messages)

    runner.messages.extend([
        {"ts": 1, "role": "assistant",
         "text": "I'm taking the required final visual check."},
        {"ts": 2, "role": "action", "text": "Took screenshot"},
        {"ts": 3, "role": "assistant",
         "text": "confirmed: the package — the screen shows the paper route."},
    ])
    runner._transcript.append({"role": "assistant",
                               "text": "confirmed: the package — the screen shows the paper route."})

    runner._finish_verify_gate()

    assistants = [m["text"] for m in runner.messages if m["role"] == "assistant"]
    assert assistants == [original]
    assert any(m["role"] == "action" for m in runner.messages)
    assert runner._transcript[-1] == {"role": "assistant", "text": original}


def test_read_only_turn_never_gates(runner):
    runner.messages.append({"ts": 0, "role": "assistant", "text": "It's $42."})
    assert runner._completion_gate_check() == ""


def test_browser_task_without_playwright_fails_without_a_retry_turn(runner):
    runner.surface = "browser"
    runner._browser_required = True
    runner._browser_tool_calls = 0
    runner.messages.append({"ts": 0, "role": "assistant",
                            "text": "I found five wineries."})

    gate = runner._completion_gate_check()

    assert gate == ""
    assert runner._gate_fired is False
    assert runner._browser_contract_failed is True
    assert runner._resolve_terminal(0) == (
        "error", "browser contract: no Playwright tool call")
    assert not any(m["role"] == "assistant" for m in runner.messages)
    assert any("visible browser" in m["text"].lower()
               for m in runner.messages if m["role"] == "error")


def test_browser_task_with_playwright_satisfies_completion_contract(runner):
    runner.surface = "browser"
    runner._browser_required = True
    runner._browser_tool_calls = 0
    runner._note_action("mcp__playwright__browser_snapshot", {})
    runner.messages.append({"ts": 0, "role": "assistant", "text": "Found it."})

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
