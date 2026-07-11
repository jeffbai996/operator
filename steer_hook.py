"""steer_hook.py — PostToolUse hook: deliver queued steers to the LIVE agent.

Wired into each operator bot cwd's .claude/settings.json (written idempotently
by operator_agent._ensure_steer_hook_settings). After every tool call the
spawned claude runs this; when the cockpit queued a steer, it comes back as
additionalContext and lands in the model's context mid-loop — no restart, no
interrupt. Empty queue → silent exit 0 (the overwhelmingly common case, so
this must stay fast and must NEVER crash: hook stderr noise pollutes the run).

Verified live on claude CLI 2.1.207 (2026-07-11): the steered instruction was
obeyed within the same agentic loop, and a project-level settings file — unlike
the --settings flag — does not break --resume.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import operator_steer
    steers = operator_steer.take_all()
    if steers:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": operator_steer.format_context(steers)}}))
except Exception:  # noqa: BLE001 — a broken hook must never break the run
    pass
