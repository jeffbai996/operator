"""Cockpit preferences that the SERVER has to know about.

Most cockpit settings are localStorage and never leave the browser. The
homepage cannot be: the last-tab reset and every new tab are opened by Python,
so the value has to live where that code can read it (the owner 2026-08-07, asking
for the default page to be a setting in the hamburger menu).

The scheme guard is the part worth pinning. This URL is handed to the
operator's own Chrome, so a stored `javascript:` or `data:` value would run in
whatever page the reset lands on — a stored-injection sink reachable by anyone
who can POST the setting.
"""
from __future__ import annotations

import json

import pytest

import operator_prefs as P


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_PATH", str(tmp_path / "prefs.json"))
    P._cache_clear()
    yield
    P._cache_clear()


def test_homepage_defaults_when_nothing_is_stored():
    assert P.homepage() == P.DEFAULT_HOMEPAGE


def test_homepage_round_trips():
    P.set_homepage("https://news.ycombinator.com")
    assert P.homepage() == "https://news.ycombinator.com"
    P._cache_clear()                      # prove it PERSISTED, not just cached
    assert P.homepage() == "https://news.ycombinator.com"


def test_bare_host_gets_https():
    """Typing "example.com" in a settings box is the common case; it must not
    become a relative path Chrome resolves against the current page."""
    P.set_homepage("example.com")
    assert P.homepage() == "https://example.com"


def test_javascript_and_data_urls_are_refused():
    """The stored value is navigated to in the operator's own browser."""
    for bad in ("javascript:alert(1)", "JavaScript:alert(1)",
                "data:text/html,<script>alert(1)</script>",
                "file:///etc/passwd", "chrome://settings"):
        with pytest.raises(ValueError):
            P.set_homepage(bad)
    assert P.homepage() == P.DEFAULT_HOMEPAGE


def test_empty_resets_to_the_default():
    P.set_homepage("https://example.com")
    P.set_homepage("")
    assert P.homepage() == P.DEFAULT_HOMEPAGE


def test_absurdly_long_url_is_refused():
    with pytest.raises(ValueError):
        P.set_homepage("https://example.com/" + "a" * 3000)


def test_unreadable_store_falls_back_instead_of_raising(monkeypatch, tmp_path):
    """A corrupt prefs file must not take the cockpit down with it — the tab
    that cannot find its homepage still has to open."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(P, "_PATH", str(bad))
    P._cache_clear()
    assert P.homepage() == P.DEFAULT_HOMEPAGE


def test_write_is_atomic(tmp_path, monkeypatch):
    """tmp+rename, so a crash mid-write cannot leave a half-file that the next
    read rejects."""
    P.set_homepage("https://example.com")
    on_disk = json.loads(open(P._PATH, encoding="utf-8").read())
    assert on_disk["homepage"] == "https://example.com"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
