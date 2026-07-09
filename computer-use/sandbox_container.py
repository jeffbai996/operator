"""sandbox_container — a REAL isolated Linux desktop for the Operator sandbox.

The old "sandbox" was an Xvfb display on the host WSL: not isolated at all (any
app it ran had the host user's filesystem, network, and credentials). This runs
the desktop inside a Docker container instead — its own rootfs, network/PID
namespace, and a non-root user. Nothing it does can reach the host.

The host drives it exactly like the local Xvfb path, but through `docker exec` —
with PERSISTENT pipes, because a fresh exec per frame/action costs ~100ms each
on WSL and made the feed ~1fps and manual steer visibly laggy:
  - live feed: ONE long-lived `docker exec ffmpeg -f x11grab … -f mjpeg -`
    (open_stream) that the feed thread reads JPEG-by-JPEG. Falls back to
    per-frame scrot if ffmpeg is missing (older images).
  - input: ONE long-lived `docker exec sh` (started lazily); each action writes
    an `xdotool …` line + an ack echo and waits for the ack — synchronous like
    the old per-exec path, at pipe latency instead of exec latency.
  - agent screenshots: still per-call scrot PNG (`screenshot()`) — the model
    wants full-quality stills, and one exec per model turn is noise.

Lifecycle (design rule): the container is PERSISTENT. It is created on first use
and survives leaving Operator, restarts, and idle — `--restart unless-stopped`.
It is only torn down by an EXPLICIT delete (`delete()` / the UI's delete action),
never by merely switching surfaces or closing the page.

One job per file: container lifecycle + capture + input for the sandbox surface.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
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
# breach isolation) — the home dir persists in a NAMED VOLUME instead, so an
# image upgrade (rm container + recreate) keeps the user's files. The two-tap
# Delete stays a true factory reset: delete() removes the volume too.
HOME_VOLUME = f"{CONTAINER}-home"
_RUN_ARGS = [
    "--name", CONTAINER,
    "--detach",
    "--restart", "unless-stopped",     # survive host/docker restarts + idle
    "--memory", "3g",              # XFCE session + chromium need headroom over 2g
    "--cpus", "2",
    "--shm-size", "512m",              # chromium needs shared memory
    "--security-opt", "no-new-privileges",
    "-v", f"{HOME_VOLUME}:/home/opuser",
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
    container, its writable layer AND the home volume (a factory reset means the
    files go too). A fresh `ensure()` starts a clean desktop."""
    _run(["rm", "-f", CONTAINER], timeout=20, check=False)
    _run(["volume", "rm", HOME_VOLUME], timeout=20, check=False)


# ── live stream (the feed's frame source) ────────────────────────────────────
# Marker in the ffmpeg arg list so we can find + kill the feed process INSIDE
# the container by name (docker exec doesn't forward host-side signals to the
# exec'd process — see stop_stream). Unique enough not to match anything else.
_FEED_TAG = "op_feed_stream"


def open_stream(fps: int = 15, quality: int = 8) -> subprocess.Popen:
    """One long-lived MJPEG pipe out of the container: ffmpeg grabs :1 (pointer
    drawn) and writes concatenated JPEGs to stdout. The caller owns the process
    (read .stdout, stop_stream() when done). Raises SandboxError if it dies at
    once — e.g. an old image without ffmpeg — so the caller can fall back to scrot.

    15fps/q8 measured: ~34KB/frame, ~480 KB/s on the wire — double the old
    8fps/q6 cadence (the choppy-slideshow feel) for +60% bandwidth, trivial
    over a LAN/tunnel. `-fflags nobuffer -flags low_delay` stop ffmpeg holding a
    frame in the mux queue → each JPEG hits stdout the instant it's encoded
    (cuts a frame-time of glass-to-glass latency).

    LEAK GUARD: reap any prior feed ffmpeg INSIDE the container before starting a
    new one. `docker exec` runs ffmpeg in the container, but a host-side
    Popen.kill() only kills the exec *client* — the container-side ffmpeg orphans
    and keeps grabbing X11 (N stacked grabs = 'persistent lag'). So we guarantee
    at-most-one feed here, and stop_stream reaches inside to kill it."""
    ensure()
    # kill any orphaned prior feed first (idempotent; no-op if none). -9: a feed
    # ffmpeg has no state worth flushing, and plain SIGTERM makes it do a clean
    # mux-shutdown that lingers ~1s — a hard kill is instant and correct here.
    _run(["exec", "-u", "opuser", CONTAINER, "pkill", "-9", "-f", _FEED_TAG],
         timeout=5, check=False)
    p = subprocess.Popen(
        [_docker(), "exec", "-u", "opuser", "-e", f"DISPLAY={DISPLAY}", CONTAINER,
         "ffmpeg", "-nostdin", "-loglevel", "quiet",
         "-fflags", "nobuffer", "-flags", "low_delay",
         "-f", "x11grab", "-draw_mouse", "1",
         "-video_size", f"{SCREEN_W}x{SCREEN_H}", "-framerate", str(fps),
         "-i", DISPLAY, "-f", "mjpeg", "-q:v", str(quality),
         "-metadata", f"comment={_FEED_TAG}", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    if p.poll() is not None:
        raise SandboxError("ffmpeg stream did not start (image missing ffmpeg?)")
    return p


def stop_stream(proc: subprocess.Popen | None) -> None:
    """Tear down a feed opened by open_stream — BOTH ends. Killing the host-side
    exec client (proc) does NOT stop the ffmpeg inside the container, so we also
    `pkill -f` the tagged process in the container. Without this second step every
    surface-switch / reconnect leaked one ffmpeg that kept grabbing X11 → the feed
    got progressively laggier the longer a session ran. Idempotent + best-effort."""
    if proc is not None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        _run(["exec", "-u", "opuser", CONTAINER, "pkill", "-9", "-f", _FEED_TAG],
             timeout=5, check=False)
    except Exception:  # noqa: BLE001 — container may be gone; nothing to reap
        pass


def split_jpegs(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split an MJPEG byte buffer into complete JPEG frames + the unfinished
    tail. Pure function (unit-tested); frames are SOI(FFD8)…EOI(FFD9) spans."""
    frames = []
    while True:
        soi = buf.find(b"\xff\xd8")
        if soi < 0:
            return frames, b""            # no frame start — drop garbage
        eoi = buf.find(b"\xff\xd9", soi + 2)
        if eoi < 0:
            return frames, buf[soi:]      # incomplete frame — keep as tail
        frames.append(buf[soi:eoi + 2])
        buf = buf[eoi + 2:]


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


# One long-lived `docker exec sh` per process: xdotool lines go down its stdin,
# an echoed ack token comes back — synchronous (the caller's next screenshot
# can't race the click) at ~5ms instead of ~100ms of exec setup per action.
_pipe: subprocess.Popen | None = None
_pipe_lock = threading.Lock()
_ACK = "__op_ack__"


def _input_pipe() -> subprocess.Popen:
    global _pipe
    if _pipe is None or _pipe.poll() is not None:
        _pipe = subprocess.Popen(
            [_docker(), "exec", "-i", "-u", "opuser", "-e", f"DISPLAY={DISPLAY}",
             CONTAINER, "sh"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
    return _pipe


def _xdotool(args: list[str]) -> None:
    global _pipe
    line = ("xdotool " + " ".join(shlex.quote(a) for a in args)
            + f" 2>/dev/null; echo {_ACK}\n").encode()
    with _pipe_lock:
        for attempt in (1, 2):     # one retry — the pipe dies with the container
            p = _input_pipe()
            try:
                p.stdin.write(line)
                p.stdin.flush()
                while True:        # drain until our ack (xdotool itself is silent)
                    out = p.stdout.readline()
                    if not out:
                        raise OSError("input pipe closed")
                    if out.strip() == _ACK.encode():
                        return
            except OSError:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
                _pipe = None
                if attempt == 2:
                    raise SandboxError("sandbox input pipe died") from None
                ensure()           # container may have restarted underneath us


def execute(action: dict) -> None:
    """Inject one computer-use action into the sandbox via xdotool. Mirrors
    computer-use/actions.execute but runs through `docker exec`."""
    kind = action.get("action")
    coord = action.get("coordinate")

    if kind in _CLICK:
        button, count = _CLICK[kind]
        if coord:
            _xdotool(["mousemove", str(coord[0]), str(coord[1])])
        # --delay 0: xdotool defaults to a 100ms press→release sleep PER click
        # (for apps that debounce). X11 registers the button instantly, so that
        # sleep is pure latency — it made fast manual tapping stall (~106ms/tap,
        # queued behind the input lock, starving the feed). 0 → ~3ms/click.
        _xdotool(["click", "--repeat", str(count), "--delay", "0", str(button)])
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
        # --delay 0: a scroll is N wheel-clicks; the default 100ms each made a
        # 5-notch scroll a half-second stall. Wheel events don't debounce.
        _xdotool(["click", "--repeat", str(amount), "--delay", "0", str(button)])
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


# ── file exchange (the cockpit's Transfer control) ───────────────────────────
# Only these home subdirs are reachable from the cockpit — the rest of the
# container filesystem stays the agent's own business.
FILE_DIRS = ("Downloads", "Desktop", "Documents")
MAX_FILE_BYTES = 200 * 1024 * 1024


def safe_rel(rel: str) -> str:
    """Validate a cockpit-supplied path as '<FILE_DIR>/<name>' — no traversal,
    no absolute paths, no nested dirs, no hidden files. Pure function (tested).
    Returns the normalized relative path or raises SandboxError."""
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p]
    if len(parts) != 2 or parts[0] not in FILE_DIRS:
        raise SandboxError(f"path must be one of {FILE_DIRS}/<file>")
    name = parts[1]
    if name in (".", "..") or name.startswith(".") or "\x00" in name:
        raise SandboxError("bad filename")
    return f"{parts[0]}/{name}"


def put_file(host_path: str, dest_name: str) -> str:
    """Copy a host file into the sandbox user's Downloads. Returns the
    container-relative path. docker cp writes root-owned; chown after."""
    name = safe_rel(f"Downloads/{dest_name}").split("/", 1)[1]
    ensure()
    _exec(["mkdir", "-p", "/home/opuser/Downloads"], check=False)
    _run(["cp", host_path, f"{CONTAINER}:/home/opuser/Downloads/{name}"], timeout=60)
    _run(["exec", "-u", "root", CONTAINER, "chown", "opuser:opuser",
          f"/home/opuser/Downloads/{name}"], check=False)
    return f"Downloads/{name}"


def list_files() -> dict:
    """Files in the exchange dirs → {dir: [{name, size, mtime}]}. One exec."""
    ensure()
    script = (
        "for d in " + " ".join(FILE_DIRS) + "; do "
        "  [ -d \"$HOME/$d\" ] || continue; "
        "  for f in \"$HOME/$d\"/*; do "
        "    [ -f \"$f\" ] && stat -c \"$d|%n|%s|%Y\" \"$f\"; "
        "  done; "
        "done")
    r = _exec(["sh", "-c", script], timeout=15, check=False)
    out: dict = {d: [] for d in FILE_DIRS}
    for line in r.stdout.decode(errors="replace").splitlines():
        try:
            d, path, size, mtime = line.split("|")
            out[d].append({"name": os.path.basename(path),
                           "size": int(size), "mtime": int(mtime)})
        except ValueError:
            continue
    return out


def get_file(rel: str, out_dir: str) -> str:
    """Copy '<dir>/<name>' out of the sandbox → a host path (size-capped)."""
    rel = safe_rel(rel)
    ensure()
    r = _exec(["stat", "-c", "%s", f"/home/opuser/{rel}"], timeout=10)
    if int(r.stdout.decode().strip() or 0) > MAX_FILE_BYTES:
        raise SandboxError("file too large to download")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, os.path.basename(rel))
    _run(["cp", f"{CONTAINER}:/home/opuser/{rel}", out_path], timeout=120)
    return out_path


def launch(app: str) -> None:
    """Launch a GUI app inside the sandbox (detached). e.g. 'chromium', 'xfce4-terminal'."""
    extra = ["--no-sandbox", "--test-type", "--no-first-run"] if app == "chromium" else []
    _run(["exec", "-d", "-u", "opuser", "-e", f"DISPLAY={DISPLAY}", CONTAINER,
          app, *extra], check=False)


def size() -> tuple:
    return (SCREEN_W, SCREEN_H)
