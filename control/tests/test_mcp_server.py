"""Tests for mcp_server.py — the operator control MCP (perceive / game_macro /
desktop computer actions) at the JSON-RPC handler level. No stdio, no browser:
a fake surface is injected via the factory."""
import json

import numpy as np
import pytest

import mcp_server as S


class FakeSurface:
    name = "fake"

    def __init__(self):
        self.calls = []

    def frame(self):
        a = np.zeros((60, 80, 3), dtype=np.uint8)
        a[10:20, 30:50] = (40, 220, 70)      # a green blob perceive can find
        return a

    def click(self, x, y, button="left", clicks=1):
        self.calls.append(("click", x, y))

    def drag(self, x1, y1, x2, y2, duration_ms=350):
        self.calls.append(("drag", x1, y1, x2, y2))

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def type_text(self, text):
        self.calls.append(("type", text))

    def key(self, combo):
        self.calls.append(("key", combo))

    def scroll(self, x, y, direction="down", amount=3):
        self.calls.append(("scroll", direction))


def server(surface_name="browser", surface=None, tmp_path=None):
    srv = S.OperatorMCP(surface_name=surface_name, bot="testbot",
                        surface_factory=lambda name: surface or FakeSurface())
    if tmp_path is not None:
        S.events.EVENT_LOG = str(tmp_path / "events.ndjson")
    return srv


def call(srv, method, params=None, id=1):
    return srv.handle({"jsonrpc": "2.0", "id": id, "method": method,
                       "params": params or {}})


def tool_result(resp):
    assert "result" in resp, resp
    txt = next(c["text"] for c in resp["result"]["content"]
               if c["type"] == "text")
    return json.loads(txt), resp["result"].get("isError", False)


# ── protocol ─────────────────────────────────────────────────────────────────
def test_initialize_handshake():
    r = call(server(), "initialize", {"protocolVersion": "2025-06-18"})
    assert r["result"]["protocolVersion"] == "2025-06-18"
    assert r["result"]["serverInfo"]["name"] == "operator-control"
    assert "tools" in r["result"]["capabilities"]


def test_notification_returns_none():
    assert server().handle({"jsonrpc": "2.0",
                            "method": "notifications/initialized"}) is None


def test_unknown_method_errors():
    r = call(server(), "resources/list")
    assert r["error"]["code"] == -32601


def test_ping():
    assert call(server(), "ping")["result"] == {}


# ── tools/list per surface ───────────────────────────────────────────────────
def test_browser_surface_hides_computer_tool():
    r = call(server("browser"), "tools/list")
    names = {t["name"] for t in r["result"]["tools"]}
    assert names == {"perceive", "game_macro"}


def test_desktop_surface_exposes_computer_tool():
    r = call(server("desktop-sandbox"), "tools/list")
    names = {t["name"] for t in r["result"]["tools"]}
    assert names == {"perceive", "game_macro", "computer"}


# ── perceive ─────────────────────────────────────────────────────────────────
def test_perceive_inline_color_spec_finds_blob(tmp_path):
    srv = server(tmp_path=tmp_path)
    r = call(srv, "tools/call", {"name": "perceive", "arguments": {
        "specs": [{"kind": "color", "label": "blob",
                   "lo": [90, 0.3, 0.3], "hi": [150, 1.0, 1.0],
                   "min_area": 20}]}})
    data, is_err = tool_result(r)
    assert not is_err
    assert data["world_state"]["targets"][0]["label"] == "blob"
    # the blob center lands inside the painted rect
    t = data["world_state"]["targets"][0]
    assert 30 <= t["x"] <= 50 and 10 <= t["y"] <= 20


def test_perceive_records_trace_event(tmp_path):
    srv = server(tmp_path=tmp_path)
    call(srv, "tools/call", {"name": "perceive", "arguments": {}})
    log = (tmp_path / "events.ndjson").read_text().strip().splitlines()
    evt = json.loads(log[-1])
    assert evt["bot"] == "testbot" and evt["action"] == "Perceiving"


def test_perceive_unknown_map_errors_cleanly(tmp_path):
    srv = server(tmp_path=tmp_path)
    r = call(srv, "tools/call", {"name": "perceive",
                                 "arguments": {"map": "no-such-game"}})
    data, is_err = tool_result(r)
    assert is_err and "no-such-game" in data["error"]


# ── game_macro ───────────────────────────────────────────────────────────────
def test_game_macro_executes_and_returns_result(tmp_path):
    fake = FakeSurface()
    srv = server(surface=fake, tmp_path=tmp_path)
    r = call(srv, "tools/call", {"name": "game_macro", "arguments": {
        "ops": [{"op": "click_xy", "x": 5, "y": 6},
                {"op": "type", "text": "gg"}]}})
    data, is_err = tool_result(r)
    assert not is_err and data["done"] is True
    assert ("click", 5, 6) in fake.calls and ("type", "gg") in fake.calls


def test_game_macro_invalid_ops_report_not_raise(tmp_path):
    srv = server(tmp_path=tmp_path)
    r = call(srv, "tools/call", {"name": "game_macro",
                                 "arguments": {"ops": [{"op": "nope"}]}})
    data, is_err = tool_result(r)
    assert data["done"] is False
    assert data["stopped_reason"].startswith("invalid_macro")


class TreeFrameSurface(FakeSurface):
    """Frame with a blob inside the openrsc map's tree HSV band
    (H≈131, S≈0.73, V≈0.59) and above its min_area=250."""

    def frame(self):
        a = np.zeros((60, 80, 3), dtype=np.uint8)
        a[10:40, 30:60] = (40, 150, 60)      # 900 px, classic RS canopy green
        return a


def test_game_macro_with_map_perceives_via_map(tmp_path):
    # openrsc map has a 'tree' color band → the canopy blob resolves as 'tree'
    fake = TreeFrameSurface()
    srv = server(surface=fake, tmp_path=tmp_path)
    r = call(srv, "tools/call", {"name": "game_macro", "arguments": {
        "map": "openrsc",
        "ops": [{"op": "click_target", "label": "tree"}]}})
    data, is_err = tool_result(r)
    assert not is_err
    assert data["done"] is True
    assert any(c[0] == "click" for c in fake.calls)


# ── computer (desktop only) ─────────────────────────────────────────────────
def test_computer_tool_blocked_on_browser_surface(tmp_path):
    srv = server("browser", tmp_path=tmp_path)
    r = call(srv, "tools/call", {"name": "computer",
                                 "arguments": {"action": "left_click",
                                               "coordinate": [3, 4]}})
    data, is_err = tool_result(r)
    assert is_err


def test_computer_click_and_type_on_desktop(tmp_path):
    fake = FakeSurface()
    srv = server("desktop-sandbox", surface=fake, tmp_path=tmp_path)
    r = call(srv, "tools/call", {"name": "computer",
                                 "arguments": {"action": "left_click",
                                               "coordinate": [3, 4]}})
    _, is_err = tool_result(r)
    assert not is_err and ("click", 3, 4) in fake.calls
    call(srv, "tools/call", {"name": "computer",
                             "arguments": {"action": "type", "text": "hey"}})
    assert ("type", "hey") in fake.calls


def test_computer_screenshot_returns_image_block(tmp_path):
    srv = server("desktop-sandbox", tmp_path=tmp_path)
    r = call(srv, "tools/call", {"name": "computer",
                                 "arguments": {"action": "screenshot"}})
    kinds = [c["type"] for c in r["result"]["content"]]
    assert "image" in kinds


def test_surface_factory_failure_is_tool_error(tmp_path):
    def boom(name):
        raise S.SurfaceError("desktop-real needs explicit confirmation")
    srv = S.OperatorMCP(surface_name="desktop-real", bot="testbot",
                        surface_factory=boom)
    S.events.EVENT_LOG = str(tmp_path / "events.ndjson")
    r = call(srv, "tools/call", {"name": "perceive", "arguments": {}})
    data, is_err = tool_result(r)
    assert is_err and "confirmation" in data["error"]
