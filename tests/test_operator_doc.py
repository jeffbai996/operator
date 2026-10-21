"""Operator has one instructions file, OPERATOR.md (the owner 2026-10-03).

Claude Code reads CLAUDE.md and Codex reads AGENTS.md, so both names are
symlinks to OPERATOR.md: every agent loads the same text, and a rule cannot be
updated in one copy and missed in the other.
"""
import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parents[1]


def test_both_agent_entry_files_are_the_one_operator_doc():
    for name in ("CLAUDE.md", "AGENTS.md"):
        entry = HERE / name
        assert entry.is_symlink(), name
        assert os.readlink(entry) == "OPERATOR.md", name


def test_the_operator_doc_carries_the_release_gate_and_streamer_rules():
    text = (HERE / "OPERATOR.md").read_text()
    assert "RELEASING.md" in text and "OP_VERSION" in text
    assert "Never re-add the stale-stream watchdog" in text
    assert "systemctl --user restart host-app-server" not in text
