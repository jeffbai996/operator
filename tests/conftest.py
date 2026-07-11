"""Shared test bootstrap — keep ALL operator tests off the real on-disk stores.

The flight recorder hooks the runner's terminal transition and the session
store backs the live cockpit chat; without this redirect, any test that walks
an AgentRunner through running→done (or boots the cockpit page) writes junk
into the REAL ledger/session under ~/.cache/computer-use (happened 2026-07-11,
caught same night). conftest imports before every test module, so the env is
set before operator_history / operator_session bind their paths.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="op-test-stores-")
os.environ.setdefault("OPERATOR_HISTORY_PATH", os.path.join(_tmp, "history.db"))
os.environ.setdefault("OPERATOR_SESSION_PATH", os.path.join(_tmp, "session.json"))
os.environ.setdefault("OPERATOR_STEER_PATH", os.path.join(_tmp, "steer.ndjson"))
os.environ.setdefault("OPERATOR_TASKS_PATH", os.path.join(_tmp, "tasks.json"))
# NOTE: no OPERATOR_STATE_PATH here — several suites isolate the runner's
# state file by monkeypatching HOME per-test, and a suite-wide env override
# outranks that (one shared state file = cross-test transcript pollution;
# broke the prompt byte-match tests when tried). Tests that SAVE runner state
# must point r._state_path at their own tmp instead.
