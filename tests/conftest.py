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
import pytest

# Inherited service exports must never outrank test isolation.
_tmp = tempfile.mkdtemp(prefix="op-test-stores-")
os.environ["OPERATOR_HISTORY_PATH"] = os.path.join(_tmp, "history.db")
os.environ["OPERATOR_SESSION_PATH"] = os.path.join(_tmp, "session.json")
os.environ["OPERATOR_STEER_PATH"] = os.path.join(_tmp, "steer.ndjson")
os.environ["OPERATOR_TASKS_PATH"] = os.path.join(_tmp, "tasks.json")
os.environ["OPERATOR_WORKSPACE_DIR"] = os.path.join(_tmp, "workspace")
os.environ["OPERATOR_BROWSER_TABS_PATH"] = os.path.join(_tmp, "browser-tabs.json")
# Legacy process fixtures do not speak JSON-RPC. Native transport has its own
# recorded-event tests; never launch a real model from either suite.
os.environ["OPERATOR_CODEX_APP_SERVER"] = "0"
os.environ["OPERATOR_FILE_BRIDGE"] = "0"
os.environ['OPERATOR_TESTING'] = '1'


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv('OPERATOR_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    monkeypatch.setenv('OPERATOR_BROWSER_STAGING_DIR', str(tmp_path / 'staging'))
# Run-completion pings hang off the same terminal transition as the ledger, so
# any test that walks a runner to done would post to the real alerts channel on
# a host that has this exported. Blank (not setdefault) — the whole point is to
# beat an inherited value.
os.environ["OPERATOR_PING_CHANNEL"] = ""
# NOTE: no OPERATOR_STATE_PATH here — several suites isolate the runner's
# state file by monkeypatching HOME per-test, and a suite-wide env override
# outranks that (one shared state file = cross-test transcript pollution;
# broke the prompt byte-match tests when tried). Tests that SAVE runner state
# must point r._state_path at their own tmp instead.
