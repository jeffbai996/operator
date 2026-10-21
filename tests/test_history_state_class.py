"""The run-history list must not paint a run it cannot classify as done.

Runs the real histStateClass out of operator.js. Requires node; skips cleanly
when absent.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "static" / "js" / "operator.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="needs node to run the real function")


def _classes(states):
    src = JS.read_text(encoding="utf-8")
    match = re.search(r"function histStateClass\(state\)\{.*?\n(?=\s*async function loadHistory)", src, re.S)
    assert match, "histStateClass is gone from operator.js"
    script = match.group(0) + "\nconsole.log(JSON.stringify(%s.map(histStateClass)));" % json.dumps(states)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_known_terminal_states_keep_their_own_class():
    assert _classes(["done", "error", "interrupted", "idle"]) == ["done", "error", "interrupted", "idle"]


def test_a_run_with_no_state_is_not_styled_as_done():
    assert _classes([None, ""]) == ["unknown", "unknown"]


def test_a_state_the_ledger_never_wrote_is_not_styled_as_done():
    assert _classes(["paused", "success"]) == ["unknown", "unknown"]
