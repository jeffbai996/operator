"""Per-conversation ownership of tabs in the shared Operator Chrome."""

import importlib.util
from pathlib import Path


_PATH = Path(__file__).resolve().parents[1] / "browse" / "operator_browser_tabs.py"
_SPEC = importlib.util.spec_from_file_location("operator_browser_tabs", _PATH)
BT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(BT)


def test_reserve_reuses_a_live_tab(monkeypatch, tmp_path):
    registry = tmp_path / "tabs.json"
    registry.write_text('{"alpha":"T-live"}', encoding="utf-8")
    monkeypatch.setattr(BT, "_page_targets", lambda endpoint: {"T-live"})
    created = []
    monkeypatch.setattr(BT, "_new_target", lambda endpoint: created.append(endpoint))

    assert BT.reserve("alpha", "http://chrome:9222", registry) == "T-live"
    assert created == []


def test_reserve_gives_each_conversation_a_distinct_tab(monkeypatch, tmp_path):
    registry = tmp_path / "tabs.json"
    live = set()
    monkeypatch.setattr(BT, "_page_targets", lambda endpoint: set(live))

    def create(endpoint):
        tid = f"T-{len(live) + 1}"
        live.add(tid)
        return tid

    monkeypatch.setattr(BT, "_new_target", create)
    first = BT.reserve("alpha", "http://chrome:9222", registry)
    second = BT.reserve("bravo", "http://chrome:9222", registry)

    assert (first, second) == ("T-1", "T-2")
    assert BT._read_registry(registry) == {"alpha": "T-1", "bravo": "T-2"}


def test_stale_target_is_replaced_without_stealing_another_tab(monkeypatch, tmp_path):
    registry = tmp_path / "tabs.json"
    registry.write_text('{"alpha":"T-dead","bravo":"T-bravo"}', encoding="utf-8")
    monkeypatch.setattr(BT, "_page_targets", lambda endpoint: {"T-bravo", "T-free"})
    monkeypatch.setattr(BT, "_new_target", lambda endpoint: "T-new")

    assert BT.reserve("alpha", "http://chrome:9222", registry) == "T-new"
    assert BT._read_registry(registry) == {"alpha": "T-new", "bravo": "T-bravo"}


def test_release_forgets_only_the_requested_conversation(monkeypatch, tmp_path):
    registry = tmp_path / "tabs.json"
    registry.write_text('{"alpha":"T-a","bravo":"T-b"}', encoding="utf-8")
    closed = []
    monkeypatch.setattr(BT, "_close_target", lambda endpoint, tid: closed.append(tid))

    assert BT.release("alpha", "http://chrome:9222", registry, close=True) is True
    assert closed == ["T-a"]
    assert BT._read_registry(registry) == {"bravo": "T-b"}


def test_activate_foregrounds_only_the_conversations_owned_tab(monkeypatch, tmp_path):
    registry = tmp_path / "tabs.json"
    registry.write_text('{"alpha":"T-a","bravo":"T-b"}', encoding="utf-8")
    monkeypatch.setattr(BT, "_page_targets", lambda endpoint: {"T-a", "T-b"})
    activated = []
    monkeypatch.setattr(BT, "_activate_target", lambda endpoint, tid: activated.append(tid))

    assert BT.activate("alpha", "http://chrome:9222", registry) is True
    assert activated == ["T-a"]
