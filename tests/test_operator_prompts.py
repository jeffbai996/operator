"""1.0.8 R2 — the prompt extraction must not change a single byte.

tests/fixtures/prompt_snapshots.json was captured through the REAL launch
path (fake Popen recording argv) BEFORE the extraction. These tests replay
the same cases through today's code and demand byte-identical output — the
directive/persona a model sees is a contract, and silent drift here changes
agent behavior without any test noticing.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_operator_prompts.py -q
"""
import json
import os

import pytest

import operator_agent as OA
import operator_prompts as P

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "prompt_snapshots.json")


def _cases():
    with open(FIXTURE) as f:
        return json.load(f)


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPERATOR_COMPLETION_GATE", "0")
    monkeypatch.setattr(OA, "_resolve_claude", lambda: "/fake/claude")
    return OA.AgentRunner()


class _FakeProc:
    def __init__(self):
        self.stdout = iter(())
        self.returncode = 0
        self.pid = 999999

    def wait(self):
        return 0

    def poll(self):
        return 0


@pytest.mark.parametrize("entry", _cases(),
                         ids=lambda e: f"{e['case']['surface']}-demo{e['case']['demo']}-{len(e['case']['task'])}")
def test_launch_path_prompt_bytes_match_pre_refactor(runner, monkeypatch, entry):
    captured = []
    monkeypatch.setattr(OA.subprocess, "Popen",
                        lambda cmd, **kw: (captured.append(cmd), _FakeProc())[1])
    c = entry["case"]
    res = runner.start(c["bot"], c["task"], demo=c["demo"],
                       surface=c["surface"], real_ok=c["real_ok"])
    assert res["ok"], res
    runner._thread.join(timeout=15)
    assert captured, "launch never reached Popen"
    cmd = captured[0]
    assert cmd[cmd.index("-p") + 1] == entry["task_arg"]
    assert cmd[cmd.index("--append-system-prompt") + 1] == entry["persona"]


# ── direct builder behavior (cheaper to reason about than full snapshots) ────

def test_chatty_task_passes_through_unwrapped():
    assert P.wrap_task("hi", "browser", False) == "hi"


def test_browser_wrap_prepends_directive_and_keeps_task_last():
    out = P.wrap_task("Find the cheapest flight", "browser", False)
    assert out.startswith("SYSTEM DIRECTIVE")
    assert out.endswith("USER REQUEST: Find the cheapest flight")


def test_demo_browser_directive_drops_the_onepassword_hint():
    assert "1PASSWORD" in P.build_browser_directive(demo=False)
    assert "1PASSWORD" not in P.build_browser_directive(demo=True)


def test_desktop_wrap_names_the_surface():
    out = P.wrap_task("open a terminal", "desktop-sandbox", False)
    assert "surface: desktop-sandbox" in out and "LIVE DESKTOP" in out


def test_persona_desktop_swap_has_no_unfilled_placeholder():
    p = P.build_persona("You are X." + P.BROWSER_MANDATE, "desktop-sandbox", False)
    assert "{surface_flavor}" not in p
    assert "ISOLATED Linux desktop" in p
    assert "You are X." in p


def test_demo_persona_strips_squad_identity():
    p = P.build_persona("You are Claude-a." + P.BROWSER_MANDATE, "browser", True)
    assert "Claude-a" not in p


# ── 2026-08-02: frozen-page and reachability gaps (the owner — bots getting stuck
# on real sites even while screenshotting/clicking correctly). The tools
# already existed upstream in @playwright/mcp; the agent was never told to
# reach for them. ─────────────────────────────────────────────────────────

def test_browser_directive_points_to_dialog_before_giving_up():
    d = P.build_browser_directive(demo=False)
    assert "browser_handle_dialog" in d
    # the stuck-browser hand-off criterion must name the dialog check as a
    # precondition, or the model still hands off on a dismissable dialog
    handoff = d[d.index("BROWSER is clearly"):d.index("BROWSER is clearly") + 400]
    assert "browser_handle_dialog" in handoff

def test_browser_directive_points_to_hover_before_pixels():
    d = P.build_browser_directive(demo=False)
    assert "browser_hover" in d
    # must come before the vision/pixel escalation, not after — hover is the
    # cheaper thing to try first
    assert d.index("browser_hover") < d.index("VISION IS YOUR FALLBACK")

def test_browser_directive_warns_pixel_clicks_need_the_target_in_frame():
    d = P.build_browser_directive(demo=False)
    # the pre-existing "SCROLL TO FIND" paragraph already says "scroll" for
    # DOM-mode targets, so a bare substring check here would pass without
    # the new content — anchor on the pixel-click sentence specifically.
    vision = d[d.index("VISION IS YOUR FALLBACK"):]
    assert "off-screen" in vision

def test_browser_directive_is_honest_about_download_reachability():
    d = P.build_browser_directive(demo=False)
    assert "download" in d.lower()
    # must not tell the model to invent/guess a save path for a browser
    # download — that's the exact failure this line exists to prevent
    assert "invent" in d or "guess" in d
