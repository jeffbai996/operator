"""Tests for the operator saved-tasks store (#30).

Pure persistence + slug logic — no Flask, no runner. Each test points the store
at a tmp file via monkeypatch so it never touches the real ~/.cache store.
Run (same as test_operator_agent.py) from modules/operator:
  PYTHONPATH=. pytest tests/test_operator_tasks.py -q
"""
import operator_tasks as OT


def _load(tmp_path, monkeypatch):
    monkeypatch.setattr(OT, "TASKS_PATH", str(tmp_path / "operator-tasks.json"))
    return OT


def test_save_then_load_round_trips(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    slug, err = m.save_task({"name": "Morning GeoGuessr", "prompt": "Play a round"})
    assert err is None
    assert slug == "morning-geoguessr"
    got = m.get_task(slug)
    assert got["name"] == "Morning GeoGuessr"
    assert got["prompt"] == "Play a round"
    assert got["created"]           # stamped
    assert got["last_run"] is None  # never run yet


def test_empty_name_rejected(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    slug, err = m.save_task({"name": "   ", "prompt": "do it"})
    assert slug is None and err == "empty name"


def test_empty_prompt_rejected(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    slug, err = m.save_task({"name": "thing", "prompt": ""})
    assert slug is None and err == "empty prompt"


def test_slug_collision_appends_suffix(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    s1, _ = m.save_task({"name": "Check News", "prompt": "a"})
    s2, _ = m.save_task({"name": "Check News", "prompt": "b"})
    assert s1 == "check-news"
    assert s2 == "check-news-2"
    # both survive independently
    assert m.get_task(s1)["prompt"] == "a"
    assert m.get_task(s2)["prompt"] == "b"


def test_resave_same_slug_updates_in_place_and_keeps_created(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    slug, _ = m.save_task({"name": "Task", "prompt": "v1"})
    created = m.get_task(slug)["created"]
    # editing under the same slug must NOT create Task-2, and must keep created
    slug2, err = m.save_task({"slug": slug, "name": "Task", "prompt": "v2"})
    assert err is None and slug2 == slug
    assert len(m.load_tasks()) == 1
    assert m.get_task(slug)["prompt"] == "v2"
    assert m.get_task(slug)["created"] == created


def test_sites_accepts_comma_string_and_list(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    s1, _ = m.save_task({"name": "a", "prompt": "p", "sites": "x.com, y.com ,"})
    assert m.get_task(s1)["sites"] == ["x.com", "y.com"]
    s2, _ = m.save_task({"name": "b", "prompt": "p", "sites": ["z.com", "  "]})
    assert m.get_task(s2)["sites"] == ["z.com"]


def test_delete_removes_and_reports(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    slug, _ = m.save_task({"name": "gone", "prompt": "p"})
    assert m.delete_task(slug) is True
    assert m.get_task(slug) is None
    assert m.delete_task(slug) is False   # already gone


def test_mark_run_stamps_last_run(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    slug, _ = m.save_task({"name": "r", "prompt": "p"})
    assert m.get_task(slug)["last_run"] is None
    m.mark_run(slug)
    assert m.get_task(slug)["last_run"]   # now stamped
    m.mark_run("nonexistent")             # silent no-op, no raise


def test_sites_preamble_shape(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    assert m.sites_preamble([]) == ""
    assert m.sites_preamble(["a.com"]).startswith("Prefer these sites")
    assert "a.com, b.com" in m.sites_preamble(["a.com", "b.com"])


def test_load_corrupt_store_returns_empty(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    with open(m.TASKS_PATH, "w") as f:
        f.write("{ this is not json")
    assert m.load_tasks() == {}   # never raises


def test_missing_store_returns_empty(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    assert m.load_tasks() == {}   # file doesn't exist yet
