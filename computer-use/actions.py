"""actions — capture the virtual display and execute computer-use actions on it.

Bridges Anthropic's computer-use tool vocabulary (the `computer_20250124` tool's
actions: screenshot / mouse_move / left_click / type / key / scroll / …) to the
xdotool + scrot primitives that drive the Xvfb display. The model emits an action
dict; `execute()` performs it; `screenshot()` returns the resulting frame.

Kept I/O-only and pure-ish: no Anthropic API here (that's loop.py), so this layer
is unit-testable against a live display without a model in the loop.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger("computer_use.actions")

# xdotool names keys differently than the model's "key" strings; the model uses
# X keysym-ish names (Return, ctrl+a, Page_Down) which xdotool already accepts,
# so most pass through. A few common aliases get normalized.
_KEY_ALIASES = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "pagedown": "Page_Down",
    "pageup": "Page_Up",
}

# Anthropic action → number of xdotool clicks / button id.
_CLICK_BUTTONS = {
    "left_click": (1, 1), "right_click": (3, 1), "middle_click": (2, 1),
    "double_click": (1, 2), "triple_click": (1, 3),
}


class ActionError(RuntimeError):
    """An action could not be executed (bad params or xdotool failure)."""


def _display_env(display: str) -> dict:
    return {**os.environ, "DISPLAY": display}


def _xdotool(args: list[str], display: str) -> None:
    tool = shutil.which("xdotool")
    if not tool:
        raise ActionError("xdotool not installed")
    try:
        subprocess.run([tool, *args], env=_display_env(display),
                       check=True, capture_output=True, timeout=15)
    except subprocess.CalledProcessError as e:
        raise ActionError(f"xdotool {args}: {e.stderr.decode(errors='replace')}") from e
    except (subprocess.SubprocessError, OSError) as e:
        raise ActionError(f"xdotool {args}: {e}") from e


def screenshot(display: str, out_dir: str) -> str:
    """Capture the whole display to a PNG in out_dir. Returns the file path."""
    scrot = shutil.which("scrot")
    if not scrot:
        raise ActionError("scrot not installed")
    os.makedirs(out_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="cu-", suffix=".png", dir=out_dir)
    os.close(fd)
    try:
        # -o overwrite the mkstemp placeholder; -z lossless.
        subprocess.run([scrot, "-o", "-z", path], env=_display_env(display),
                       check=True, capture_output=True, timeout=15)
    except (subprocess.SubprocessError, OSError) as e:
        raise ActionError(f"scrot: {e}") from e
    return path


def _norm_key(combo: str) -> str:
    # "ctrl+a" → "ctrl+a"; "Enter" → "Return". xdotool wants '+'-joined keysyms.
    parts = combo.split("+")
    out = []
    for p in parts:
        out.append(_KEY_ALIASES.get(p.strip().lower(), p.strip()))
    return "+".join(out)


def execute(action: dict, display: str) -> None:
    """Perform one computer-use action on the display.

    `action` is the model's tool input: {"action": "left_click", "coordinate":[x,y]}
    / {"action":"type","text":"..."} / {"action":"key","text":"ctrl+a"} /
    {"action":"scroll","coordinate":[x,y],"scroll_direction":"down","scroll_amount":3}
    / {"action":"mouse_move","coordinate":[x,y]}. `screenshot` is handled by the
    caller (it needs the frame back), not here.
    """
    kind = action.get("action")
    coord = action.get("coordinate")

    if kind in _CLICK_BUTTONS:
        button, count = _CLICK_BUTTONS[kind]
        if coord:
            _xdotool(["mousemove", str(coord[0]), str(coord[1])], display)
        _xdotool(["click", "--repeat", str(count), str(button)], display)
        return

    if kind == "mouse_move":
        if not coord:
            raise ActionError("mouse_move needs a coordinate")
        _xdotool(["mousemove", str(coord[0]), str(coord[1])], display)
        return

    if kind == "type":
        text = action.get("text", "")
        _xdotool(["type", "--", text], display)
        return

    if kind == "key":
        _xdotool(["key", "--", _norm_key(action.get("text", ""))], display)
        return

    if kind == "scroll":
        if coord:
            _xdotool(["mousemove", str(coord[0]), str(coord[1])], display)
        direction = action.get("scroll_direction", "down")
        amount = int(action.get("scroll_amount", 3))
        button = {"up": 4, "down": 5, "left": 6, "right": 7}.get(direction, 5)
        _xdotool(["click", "--repeat", str(amount), str(button)], display)
        return

    if kind in ("left_mouse_down", "left_mouse_up"):
        op = "mousedown" if kind == "left_mouse_down" else "mouseup"
        _xdotool([op, "1"], display)
        return

    if kind == "wait":
        # The loop sleeps; nothing to inject. Recognized so it's not "unknown".
        return

    raise ActionError(f"unsupported action: {kind!r}")
