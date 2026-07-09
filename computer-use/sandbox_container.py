"""sandbox_container — a REAL isolated Linux desktop for the Operator sandbox.

The old "sandbox" was an Xvfb display on the host WSL: not isolated at all (any
app it ran had the host user's filesystem, network, and credentials). This runs
the desktop inside a Docker container instead — its own rootfs, network/PID
namespace, and a non-root user. Nothing it does can reach the host.

The host drives it exactly like the local Xvfb path, but through `docker exec`:
  - capture: `docker exec <c> scrot <tmp>` → copy the PNG out → return its path
  - input:   `docker exec <c> xdotool <...>` (same action dicts as actions.py)

Lifecycle (design rule): the container is PERSISTENT. It is created on first use
and survives leaving Operator, restarts, and idle — `--restart unless-stopped`.
It is only torn down by an EXPLICIT delete (`delete()` / the UI's delete action),
never by merely switching surfaces or closing the page.

One job per file: container lifecycle + capture + input for the sandbox surface.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

CONTAINER = os.environ.get("OPERATOR_SANDBOX_CONTAINER", "operator-sandbox")
IMAGE = os.environ.get("OPERATOR_SANDBOX_IMAGE", "operator-sandbox:latest")
DISPLAY = ":1"                       # the container's internal Xvfb display
# XGA, deliberately: Claude's coordinate grounding is calibrated around
# 1024x768 (Anthropic's computer-use reference container runs exactly this) —
# at 1280x800 clicks on dense targets (calendar grids) landed visibly off.
GEOMETRY = "1024x768x24"
SCREEN_W, SCREEN_H = 1024, 768

# Resource + safety caps for the container. No host bind-mounts (that would
# breach isolation); non-root user is baked into the image.
_RUN_ARGS = [
    "--name", CONTAINER,
    "--detach",
    "--restart", "unless-stopped",     # survive host/docker restarts + idle
    "--memory", "2g",
    "--cpus", "2",
    "--shm-size", "512m",              # chromium needs shared memory
    "--security-opt", "no-new-privileges",
]


class SandboxError(RuntimeError):
    """Docker/desktop couldn't be brought up, or a docker exec failed."""


def _docker() -> str:
    d = shutil.which("docker")
    if not d:
        raise SandboxError("docker is not installed on this host")
    return d


def _run(args: list[str], timeout: float = 15, check: bool = True) -> subprocess.CompletedProcess:
    """Run a docker CLI command. Raises SandboxError on failure when check."""
    try:
        r = subprocess.run([_docker(), *args], capture_output=True,
                           timeout=timeout, check=False)
    except (subprocess.SubprocessError, OSError) as e:
        raise SandboxError(f"docker {args[0]}: {e}") from e
    if check and r.returncode != 0:
        raise SandboxError(
            f"docker {args[0]} failed: {r.stderr.decode(errors='replace').strip()}")
    return r


def _exec(cmd: list[str], timeout: float = 15, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command INSIDE the container as the desktop user, with DISPLAY set."""
    return _run(["exec", "-u", "opuser", "-e", f"DISPLAY={DISPLAY}", CONTAINER, *cmd],
                timeout=timeout, check=check)


# ── lifecycle ────────────────────────────────────────────────────────────────
def state() -> str:
    """'running' | 'stopped' (exists but not up) | 'absent' (no container)."""
    r = _run(["inspect", "-f", "{{.State.Running}}", CONTAINER], check=False)
    if r.returncode != 0:
        return "absent"
    return "running" if r.stdout.decode().strip() == "true" else "stopped"


def _display_live() -> bool:
    r = _exec(["xdpyinfo"], timeout=6, check=False)
    return r.returncode == 0


def ensure(timeout: float = 30) -> str:
    """Ensure the sandbox container is running and its desktop is up. Idempotent.

    Creates the container on first use; starts it if it exists but is stopped;
    no-ops if already running. Returns the container name. Raises SandboxError
    if the image is missing or the desktop never comes up.
    """
    st = state()
    if st == "absent":
        # image present?
        img = _run(["image", "inspect", IMAGE], check=False)
        if img.returncode != 0:
            raise SandboxError(
                f"sandbox image {IMAGE!r} not found — build it "
                f"(docker build -t {IMAGE} <context>)")
        _run(["run", *_RUN_ARGS, IMAGE])
    elif st == "stopped":
        _run(["start", CONTAINER])
    # wait for the internal desktop to accept X connections
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _display_live():
            return CONTAINER
        time.sleep(0.4)
    raise SandboxError("sandbox desktop did not come up in time")


def stop() -> None:
    """Stop the container but KEEP it (state persists; `start` resumes it).
    Used for a soft pause — NOT the destructive delete."""
    if state() != "absent":
        _run(["stop", CONTAINER], timeout=20, check=False)


def delete() -> None:
    """EXPLICIT teardown — the only thing that destroys the sandbox. Removes the
    container and its writable layer. A fresh `ensure()` starts a clean desktop."""
    _run(["rm", "-f", CONTAINER], timeout=20, check=False)


# ── capture ──────────────────────────────────────────────────────────────────
def screenshot(out_dir: str) -> str:
    """Capture the sandbox desktop → a PNG on the HOST, returning its path.
    scrot writes inside the container; we stream it out via `docker cp`-to-stdout
    (`docker exec cat`) so no shared volume is needed."""
    os.makedirs(out_dir, exist_ok=True)
    in_path = "/tmp/op-sandbox-frame.png"
    # -p draws the mouse pointer into the frame — without it the agent has no
    # feedback on where its last click landed, so a missed click can't be
    # corrected (it just guesses again in another direction).
    _exec(["scrot", "-po", in_path], timeout=10)
    r = _run(["exec", CONTAINER, "cat", in_path], timeout=10)
    out_path = os.path.join(out_dir, "sandbox.png")
    with open(out_path, "wb") as f:
        f.write(r.stdout)
    return out_path


# ── input (same action dicts as actions.execute) ─────────────────────────────
_CLICK = {"left_click": (1, 1), "double_click": (1, 2), "triple_click": (1, 3),
          "right_click": (3, 1), "middle_click": (2, 1)}


def _norm_key(combo: str) -> str:
    # xdotool accepts most X keysyms; map the few the model spells differently.
    m = {"Enter": "Return", "Return": "Return", "Esc": "Escape"}
    parts = [m.get(p, p) for p in combo.split("+")]
    return "+".join(parts)


def _xdotool(args: list[str]) -> None:
    _exec(["xdotool", *args], timeout=10)


def execute(action: dict) -> None:
    """Inject one computer-use action into the sandbox via xdotool. Mirrors
    computer-use/actions.execute but runs through `docker exec`."""
    kind = action.get("action")
    coord = action.get("coordinate")

    if kind in _CLICK:
        button, count = _CLICK[kind]
        if coord:
            _xdotool(["mousemove", str(coord[0]), str(coord[1])])
        _xdotool(["click", "--repeat", str(count), str(button)])
        return
    if kind == "mouse_move":
        if not coord:
            raise SandboxError("mouse_move needs a coordinate")
        _xdotool(["mousemove", str(coord[0]), str(coord[1])])
        return
    if kind == "type":
        _xdotool(["type", "--", action.get("text", "")])
        return
    if kind == "key":
        _xdotool(["key", "--", _norm_key(action.get("text", ""))])
        return
    if kind == "scroll":
        if coord:
            _xdotool(["mousemove", str(coord[0]), str(coord[1])])
        amount = int(action.get("scroll_amount", 3))
        button = {"up": 4, "down": 5, "left": 6, "right": 7}.get(
            action.get("scroll_direction", "down"), 5)
        _xdotool(["click", "--repeat", str(amount), str(button)])
        return
    if kind in ("left_mouse_down", "left_mouse_up"):
        _xdotool(["mousedown" if kind == "left_mouse_down" else "mouseup", "1"])
        return
    if kind in ("key_down", "key_up"):
        # held keys from manual steer (arrows etc.) — press without release so
        # the desktop sees a genuine hold, released by the matching key_up
        _xdotool(["keydown" if kind == "key_down" else "keyup", "--",
                  _norm_key(action.get("text", ""))])
        return
    if kind == "left_click_drag":
        start = action.get("start_coordinate")
        if start:
            _xdotool(["mousemove", str(start[0]), str(start[1])])
        _xdotool(["mousedown", "1"])
        if coord:
            _xdotool(["mousemove", str(coord[0]), str(coord[1])])
        _xdotool(["mouseup", "1"])
        return
    if kind == "wait":
        return
    raise SandboxError(f"unknown action {kind!r}")


def launch(app: str) -> None:
    """Launch a GUI app inside the sandbox (detached). e.g. 'chromium', 'xfce4-terminal'."""
    extra = ["--no-sandbox", "--no-first-run"] if app == "chromium" else []
    _run(["exec", "-d", "-u", "opuser", "-e", f"DISPLAY={DISPLAY}", CONTAINER,
          app, *extra], check=False)


def size() -> tuple:
    return (SCREEN_W, SCREEN_H)
