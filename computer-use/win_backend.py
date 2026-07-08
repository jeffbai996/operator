"""win_backend — drive the REAL Windows desktop from WSL (computer-use option A).

The screen-agnostic agentic loop in loop.py speaks one small interface to
whatever backend it's pointed at: ensure() / screen_size() / screenshot() /
execute(). The Linux backend (display.py + actions.py) drives an isolated Xvfb
display via scrot+xdotool; THIS backend drives the owner's real Windows desktop
via PowerShell — `win_capture.ps1` (System.Drawing screen grab) for screenshots
and `win_input.ps1` (Win32 SetCursorPos + mouse_event + SendKeys) for input,
both exec'd through powershell.exe across the WSL→Windows boundary.

INVASIVE BY NATURE: this moves the owner's actual cursor and types into their
live session — they can't use the machine while a loop runs, and a misclick
acts on their real desktop. That's the option-A tradeoff (vs the
safe-but-isolated Linux sandbox). The vision loop itself is identical to
option B — only capture+inject differ, which is the whole reason B was built
first.

Feasibility verified 2026-06-25: screenshot (1609x1109 real desktop) and cursor
injection (cursor physically moved) both confirmed from WSL.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile

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


class WinBackendError(RuntimeError):
    """A PowerShell capture/inject call failed."""


def _pwsh(args: list[str], ps1: str) -> str:
    """Run a vendored .ps1 (Windows path) via powershell.exe and return stdout."""
    win_ps1 = subprocess.run(["wslpath", "-w", ps1], check=True,
                             capture_output=True, text=True).stdout.strip()
    cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", win_ps1, *args]
    try:
        r = subprocess.run(cmd, check=True, capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
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
# measured sweet spot on a 2816x1940 high-DPI panel: ~1482 input tokens/frame
# (−32% vs 1568) while keeping dense UI text (tickers, terminals) legible to the model.
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
        _pwsh(["-Action", win_action, "-X", str(x), "-Y", str(y)], _INPUT_PS1)
        return
    if kind == "type":
        _pwsh(["-Action", "type", "-Text", action.get("text", "")], _INPUT_PS1)
        return
    if kind == "key":
        _pwsh(["-Action", "key", "-Key", _sendkeys(action.get("text", ""))], _INPUT_PS1)
        return
    if kind == "scroll":
        direction = action.get("scroll_direction", "down")
        amount = int(action.get("scroll_amount", 3))
        signed = amount if direction == "up" else -amount
        _pwsh(["-Action", "scroll", "-X", str(x), "-Y", str(y),
               "-Amount", str(signed)], _INPUT_PS1)
        return
    if kind in ("wait", "screenshot"):
        return
    raise WinBackendError(f"unsupported action: {kind!r}")
