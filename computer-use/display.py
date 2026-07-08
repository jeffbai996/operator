"""display — manage the isolated Xvfb virtual display the computer-use loop drives.

Option B of the computer-use design (2026-06-25): rather than driving the
owner's real Windows desktop (invasive — moves their live cursor), the agent
gets its OWN headless Linux X display. Xvfb renders into a memory framebuffer
(no physical screen), a lightweight WM (openbox) gives windows somewhere to
live, and scrot/xdotool capture + drive it. Safe and isolated: the agent can't
touch anything outside this sandbox, and it never fights the owner for the
mouse.

This module owns the display lifecycle only — start it, confirm it's live, tear it
down. Capture + input live in `actions.py`; the agentic loop in `loop.py`.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger("computer_use.display")

# :99 by default — high number, unlikely to collide with a real X session (there
# is none on headless WSL, but be polite). Overridable for parallel sandboxes.
DEFAULT_DISPLAY = os.environ.get("COMPUTER_USE_DISPLAY", ":99")
DEFAULT_GEOMETRY = os.environ.get("COMPUTER_USE_GEOMETRY", "1280x800x24")


class DisplayError(RuntimeError):
    """Xvfb / WM couldn't be brought up, or a required binary is missing."""


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise DisplayError(
            f"`{binary}` not found. Install the X stack: "
            "sudo apt-get install -y xvfb x11-utils xdotool scrot openbox"
        )
    return path


def is_live(display: str = DEFAULT_DISPLAY) -> bool:
    """True if an X server answers on `display` (xdpyinfo succeeds)."""
    xdpyinfo = shutil.which("xdpyinfo")
    if not xdpyinfo:
        return False
    try:
        r = subprocess.run(
            [xdpyinfo], env={**os.environ, "DISPLAY": display},
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def ensure(display: str = DEFAULT_DISPLAY,
           geometry: str = DEFAULT_GEOMETRY) -> str:
    """Bring up Xvfb + openbox on `display` if not already live. Idempotent.

    Returns the display string (e.g. ":99"). Raises DisplayError if it can't be
    started. The Xvfb/openbox processes are detached (start_new_session) so they
    outlive this call — the loop reattaches via DISPLAY each invocation.
    """
    _require("Xvfb")
    _require("openbox")
    if is_live(display):
        log.debug("display %s already live", display)
        return display

    # -nolisten tcp: framebuffer only, no network X (smaller attack surface).
    xvfb = _require("Xvfb")
    subprocess.Popen(
        [xvfb, display, "-screen", "0", geometry, "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Wait for the server to accept connections before launching the WM.
    deadline = time.time() + 10
    while time.time() < deadline:
        if is_live(display):
            break
        time.sleep(0.25)
    else:
        raise DisplayError(f"Xvfb did not come up on {display} within 10s")

    openbox = _require("openbox")
    subprocess.Popen(
        [openbox],
        env={**os.environ, "DISPLAY": display},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    log.info("display %s up (%s) + openbox", display, geometry)
    return display


def screen_size(geometry: str = DEFAULT_GEOMETRY) -> tuple[int, int]:
    """(width, height) parsed from a WxHxDepth geometry string."""
    w, h, *_ = geometry.split("x")
    return int(w), int(h)
