"""win_backend — drive the REAL Windows desktop from WSL (computer-use option A).

The screen-agnostic agentic loop in loop.py speaks one small interface to
whatever backend it's pointed at: ensure() / screen_size() / screenshot() /
execute(). The Linux backend (display.py + actions.py) drives an isolated Xvfb
display via scrot+xdotool; THIS backend drives the owner's real Windows desktop via
PowerShell — `win_capture.ps1` (System.Drawing screen grab) for screenshots and
`win_input.ps1` (Win32 SetCursorPos + mouse_event + SendKeys) for input, both
exec'd through powershell.exe across the WSL→Windows boundary.

INVASIVE BY NATURE: this moves the owner's actual cursor and types into their live
session — they can't use the machine while a loop runs, and a misclick acts on their
real desktop. That's the option-A tradeoff (vs the safe-but-isolated Linux
sandbox). The vision loop itself is identical to option B — only capture+inject
differ, which is the whole reason B was built first.

Feasibility verified 2026-06-25: screenshot (1609x1109 real desktop) and cursor
injection (cursor physically moved) both confirmed from WSL.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid

log = logging.getLogger("computer_use.win_backend")

# Resolve powershell.exe ABSOLUTELY: under a systemd --user unit (the operator
# server and anything it spawns) the WSL interop dirs are not on PATH, so a bare
# "powershell.exe" fails there while working fine in a login shell.
_PS_CANONICAL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
POWERSHELL = (shutil.which("powershell.exe")
              or (_PS_CANONICAL if os.path.exists(_PS_CANONICAL)
                  else "powershell.exe"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_CAPTURE_PS1 = os.path.join(_HERE, "win_capture.ps1")
_INPUT_PS1 = os.path.join(_HERE, "win_input.ps1")

# Input is intentionally NOT launched through WSL interop.  Windows will let an
# interop child capture the live screen but denies its window-station write
# access, so SetCursorPos/keybd_event silently do nothing.  A tiny PowerShell
# broker is registered as an InteractiveToken scheduled task and consumes this
# user-private file queue from the real input desktop.
_BROKER_DIR_CACHE: str | None = None
_BROKER_HEARTBEAT_MAX_AGE = 3.0

# Windows temp dir the screenshots land in (readable from WSL via /mnt/c).
# Resolved once from the Windows %TEMP% so it works regardless of the login user.
_WIN_TEMP_CACHE: str | None = None

# SendKeys uses its own escape syntax; map the model's key names to it.
# https://learn.microsoft.com/dotnet/api/system.windows.forms.sendkeys
_KEY_TO_SENDKEYS = {
    "Return": "{ENTER}", "enter": "{ENTER}", "Escape": "{ESC}", "esc": "{ESC}",
    "Tab": "{TAB}", "tab": "{TAB}", "BackSpace": "{BACKSPACE}",
    "Delete": "{DELETE}", "space": " ", "Page_Down": "{PGDN}",
    "Page_Up": "{PGUP}", "Up": "{UP}", "Down": "{DOWN}",
    "Left": "{LEFT}", "Right": "{RIGHT}", "Home": "{HOME}", "End": "{END}",
}
_MODIFIER_TO_SENDKEYS = {"ctrl": "^", "control": "^", "alt": "%", "shift": "+"}
# SendKeys cannot express the Windows key — combos carrying one of these route
# through win_input.ps1's `hotkey` action (raw keybd_event VKs) instead. The
# old path silently DROPPED the Win modifier: "switch virtual desktop"
# (win+ctrl+right) actually sent ctrl+right into the void (found 2026-07-11).
_VK_ROUTED_MODS = {"win", "windows", "super", "meta", "cmd"}


class WinBackendError(RuntimeError):
    """A PowerShell capture/inject call failed."""


_INTEROP_CACHE: str | None = None


def _os_stat_sock(path: str) -> bool:
    """True if path is a live socket."""
    import stat as _stat
    try:
        return _stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


# Read-only probe: the cursor position from the INTERACTIVE session is a real
# non-zero point; from a detached window station it comes back 0,0. One line,
# no P/Invoke here-string (that's terminator-column-fragile over -Command).
_INTEROP_PROBE = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$p=[System.Windows.Forms.Cursor]::Position;"
                  'Write-Output "$($p.X),$($p.Y)"')


def _interop_reaches_session(sock: str) -> bool:
    """True if launching a Windows process through this WSL_INTEROP socket lands
    in the INTERACTIVE desktop session. Probe: Cursor.Position returns the real
    cursor point (non-0,0) from the interactive session, but 0,0 from a detached
    window station. Cheap, read-only — never moves anything."""
    env = os.environ.copy()
    env["WSL_INTEROP"] = sock
    try:
        r = subprocess.run([POWERSHELL, "-NoProfile", "-Command", _INTEROP_PROBE],
                           capture_output=True, text=True, timeout=8,
                           stdin=subprocess.DEVNULL, env=env)
        out = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        pos = out[-1] if out else ""
        return bool(pos) and pos != "0,0"
    except (subprocess.SubprocessError, OSError):
        return False


def _live_interop(reprobe: bool = False) -> str | None:
    """A WSL↔Windows interop socket that reaches the INTERACTIVE desktop session.

    WSL_INTEROP is the socket that makes a WSL process launch Windows binaries in
    the interactive Windows session. A systemd --user unit (how the operator
    server runs) inherits NO WSL_INTEROP — so its powershell.exe spawns land in a
    detached window station: GDI screen CAPTURE still works (it reads the display
    regardless), but SetCursorPos / mouse_event / SendKeys hit the wrong station
    and NOTHING moves. That's the "desktop control silently does nothing, yet the
    feed looks fine" break (2026-07-12).

    NOT every live socket reaches the session — a detached/dead session leaves a
    live socket whose spawns land nowhere (verified: newest-by-mtime grabbed a
    socket where GetCursorPos returned 0,0 and the cursor never moved, while an
    older socket worked). So we PROBE each candidate (newest first) for session
    reachability and cache the winner. Re-probe on demand when a cached socket
    stops working. Returns None if nothing reaches the session (→ fall back to
    inherited env, no worse than before)."""
    global _INTEROP_CACHE
    if _INTEROP_CACHE and not reprobe and _os_stat_sock(_INTEROP_CACHE):
        return _INTEROP_CACHE
    # own env first if it actually reaches the session
    env_sock = os.environ.get("WSL_INTEROP")
    candidates: list[str] = []
    if env_sock and _os_stat_sock(env_sock):
        candidates.append(env_sock)
    try:
        socks = []
        for name in os.listdir("/run/WSL"):
            if not name.endswith("_interop"):
                continue
            p = os.path.join("/run/WSL", name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            import stat as _stat
            if _stat.S_ISLNK(st.st_mode):   # skip the 1_interop→2_interop symlinks
                continue
            socks.append((st.st_mtime, p))
        socks.sort(reverse=True)
        candidates += [p for _mt, p in socks if p not in candidates]
    except OSError:
        pass
    for sock in candidates:
        if _os_stat_sock(sock) and _interop_reaches_session(sock):
            _INTEROP_CACHE = sock
            return sock
    _INTEROP_CACHE = None
    return None


def _pwsh(args: list[str], ps1: str) -> str:
    """Run a vendored .ps1 (Windows path) via powershell.exe and return stdout."""
    win_ps1 = subprocess.run(["wslpath", "-w", ps1], check=True,
                             capture_output=True, text=True).stdout.strip()
    cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", win_ps1, *args]
    # Ensure the Windows child lands in the INTERACTIVE session so input actually
    # reaches the owner's desktop (see _live_interop). No-op if the inherited env is
    # already good; harmless best-effort if no live socket is found.
    _env = os.environ.copy()
    _sock = _live_interop()
    if _sock:
        _env["WSL_INTEROP"] = _sock
    try:
        r = subprocess.run(cmd, check=True, capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL, env=_env)
    except subprocess.CalledProcessError as e:
        raise WinBackendError(f"powershell {os.path.basename(ps1)} {args}: "
                              f"{e.stderr.strip()}") from e
    except (subprocess.SubprocessError, OSError) as e:
        raise WinBackendError(f"powershell {os.path.basename(ps1)}: {e}") from e
    return r.stdout.strip()


def _win_temp() -> str:
    """Windows %TEMP% as a WSL path (/mnt/c/...), cached."""
    global _WIN_TEMP_CACHE
    if _WIN_TEMP_CACHE is None:
        win = subprocess.run(
            [POWERSHELL, "-NoProfile", "-Command", "$env:TEMP"],
            check=True, capture_output=True, text=True,
            stdin=subprocess.DEVNULL).stdout.strip()
        _WIN_TEMP_CACHE = subprocess.run(["wslpath", "-u", win], check=True,
                                         capture_output=True, text=True).stdout.strip()
    return _WIN_TEMP_CACHE


# ── interface mirrored from display.py + actions.py ──────────────────────────

def ensure(_target=None) -> str:
    """No display to bring up — the Windows desktop always exists. Returns a
    sentinel target string so the loop can pass it around uniformly."""
    if not os.path.exists(_CAPTURE_PS1) or not os.path.exists(_INPUT_PS1):
        raise WinBackendError("win_capture.ps1 / win_input.ps1 missing")
    return "windows-primary"


def _broker_dir() -> str:
    """Return the WSL path shared with the interactive Windows input broker."""
    global _BROKER_DIR_CACHE
    configured = os.environ.get("COMPUTER_USE_WIN_BROKER_DIR")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    if _BROKER_DIR_CACHE is None:
        _BROKER_DIR_CACHE = os.path.join(_win_temp(), "operator-input-broker")
    return _BROKER_DIR_CACHE


def ensure_input() -> None:
    """Fail closed unless the interactive broker has a fresh heartbeat."""
    heartbeat = os.path.join(_broker_dir(), "heartbeat.json")
    try:
        age = time.time() - os.path.getmtime(heartbeat)
    except OSError as e:
        raise WinBackendError(
            "Windows input broker is not running; start the "
            "OperatorInputBroker scheduled task") from e
    if age > _BROKER_HEARTBEAT_MAX_AGE:
        raise WinBackendError(
            f"Windows input broker heartbeat is stale ({age:.1f}s); restart "
            "the OperatorInputBroker scheduled task")


def _broker_request(action: dict, timeout: float = 6.0) -> None:
    """Queue one input action and wait for the interactive broker's verdict."""
    ensure_input()
    broker = _broker_dir()
    os.makedirs(broker, mode=0o700, exist_ok=True)
    request_id = uuid.uuid4().hex
    request = os.path.join(broker, f"{request_id}.request.json")
    response = os.path.join(broker, f"{request_id}.response.json")
    fd, pending = tempfile.mkstemp(prefix=f".{request_id}.", suffix=".tmp",
                                   dir=broker)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(action, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(pending, request)
    except Exception:
        try:
            os.unlink(pending)
        except OSError:
            pass
        raise

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                # Windows PowerShell 5.1 writes an UTF-8 BOM; accept that and
                # the BOM-less responses produced by tests/other brokers.
                with open(response, encoding="utf-8-sig") as f:
                    result = json.load(f)
                if not result.get("ok"):
                    raise WinBackendError(result.get("error") or
                                          "Windows input broker rejected action")
                return
            except FileNotFoundError:
                time.sleep(0.02)
        raise WinBackendError(
            f"Windows input broker timed out after {timeout:.1f}s")
    finally:
        for path in (request, response):
            try:
                os.unlink(path)
            except OSError:
                pass


def screen_size(_target=None) -> tuple[int, int]:
    """IMAGE-space geometry of a capture (what the model sees and what the tool's
    display_width/height_px must match). A probe capture also primes the
    image→physical scale factors. NOT the physical resolution — that can be far
    larger on a high-DPI panel; execute() scales coords back up."""
    w, h = _capture_to_winpath(_win_temp_win() + "\\cu-probe.png")
    return w, h


def _win_temp_win() -> str:
    """Windows %TEMP% in Windows form (C:\\...), for passing to the .ps1."""
    return subprocess.run([POWERSHELL, "-NoProfile", "-Command", "$env:TEMP"],
                          check=True, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stdout.strip()


# Downscale captures to this long-edge width (preserve aspect). 1280 is the
# measured sweet spot on the owner's 2816x1940 panel: ~1482 input tokens/frame (−32%
# vs 1568) while keeping dense UI text (tickers, terminals) legible to the model.
# Below ~1024 small text starts to blur → misclicks → retries that cost more than
# they save. Bump up via COMPUTER_USE_WIN_MAXWIDTH for read-heavy tasks. The
# physical resolution far exceeds this on a high-DPI panel, so the model's click
# coords come back in IMAGE space and are scaled UP to physical pixels in execute().
_CAPTURE_MAX_WIDTH = int(os.environ.get("COMPUTER_USE_WIN_MAXWIDTH", "1280"))

# Scale factors set on each capture: physical_px / image_px. execute() multiplies
# the model's coords by these to hit the right spot on the real desktop.
_scale_x = 1.0
_scale_y = 1.0


def _capture_to_winpath(win_out: str) -> tuple[int, int]:
    """Capture (DPI-aware, downscaled to _CAPTURE_MAX_WIDTH). The .ps1 prints
    'img_w img_h phys_w phys_h'; record the image→physical scale and return the
    IMAGE dims (what the model sees / sizes the tool to)."""
    global _scale_x, _scale_y
    out = _pwsh([win_out, str(_CAPTURE_MAX_WIDTH)], _CAPTURE_PS1)
    nums = re.findall(r"\d+", out)
    if len(nums) < 4:
        raise WinBackendError(f"capture returned bad geometry: {out!r}")
    img_w, img_h, phys_w, phys_h = (int(n) for n in nums[:4])
    _scale_x = phys_w / img_w if img_w else 1.0
    _scale_y = phys_h / img_h if img_h else 1.0
    return img_w, img_h


def screenshot(_target: str, out_dir: str) -> str:
    """Grab the Windows desktop; return a WSL-readable JPEG path under out_dir.

    PowerShell writes a PNG to Windows %TEMP% (the AV-safe path — JPEG encoding in
    PowerShell trips Defender; see win_capture.ps1). We then re-encode it to a
    quality-80 JPEG on the WSL side via Pillow — ~10x smaller, which (with the
    prune in loop.py) keeps the multi-frame request under the API size cap.
    """
    os.makedirs(out_dir, exist_ok=True)
    win_temp_w = _win_temp_win()
    win_name = "cu-winshot.png"
    win_out = f"{win_temp_w}\\{win_name}"
    _capture_to_winpath(win_out)
    src = os.path.join(_win_temp(), win_name)
    fd, dst = tempfile.mkstemp(prefix="cu-", suffix=".jpg", dir=out_dir)
    os.close(fd)
    try:
        from PIL import Image
        im = Image.open(src).convert("RGB")
        im.save(dst, "JPEG", quality=80)
    except Exception:  # noqa: BLE001 — fall back to raw copy if Pillow absent
        with open(src, "rb") as a, open(dst, "wb") as b:
            b.write(a.read())
    return dst


def _sendkeys(combo: str) -> str:
    """Translate a model key string ('ctrl+a', 'Return') to SendKeys syntax."""
    parts = combo.split("+")
    mods = "".join(_MODIFIER_TO_SENDKEYS.get(p.strip().lower(), "")
                   for p in parts[:-1])
    last = parts[-1].strip()
    key = _KEY_TO_SENDKEYS.get(last, _KEY_TO_SENDKEYS.get(last.lower(), last))
    return mods + key


def execute(action: dict, _target: str) -> None:
    """Perform one computer-use action on the Windows desktop.

    The model's coordinates are in the (downscaled) IMAGE space it was shown; we
    scale them UP to physical pixels via the factors recorded at last capture, so
    a click on a high-DPI panel lands where the model intended."""
    kind = action.get("action")
    coord = action.get("coordinate")
    if coord:
        x = int(round(coord[0] * _scale_x))
        y = int(round(coord[1] * _scale_y))
    else:
        x, y = -1, -1

    if kind in ("left_click", "right_click", "double_click", "mouse_move"):
        win_action = "move" if kind == "mouse_move" else kind
        _broker_request({"action": win_action, "x": x, "y": y})
        return
    if kind == "type":
        _broker_request({"action": "type", "text": action.get("text", "")})
        return
    if kind == "key":
        combo = action.get("text", "")
        parts = [p.strip().lower() for p in combo.split("+")]
        if any(p in _VK_ROUTED_MODS for p in parts):
            _broker_request({"action": "hotkey", "key": combo})
        else:
            _broker_request({"action": "key", "key": _sendkeys(combo)})
        return
    if kind == "scroll":
        direction = action.get("scroll_direction", "down")
        amount = int(action.get("scroll_amount", 3))
        signed = amount if direction == "up" else -amount
        _broker_request({"action": "scroll", "x": x, "y": y,
                         "amount": signed})
        return
    if kind in ("wait", "screenshot"):
        return
    raise WinBackendError(f"unsupported action: {kind!r}")
