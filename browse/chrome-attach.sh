#!/usr/bin/env bash
# chrome-attach.sh — launch (or detect) a Chrome with remote debugging on :PORT,
# using a SEPARATE automation profile (so it doesn't touch your daily Chrome).
# Sign into your accounts ONCE in the window this opens; logins persist for Operator.
#
# Cross-platform: macOS, Linux, Windows (Git-Bash), and WSL (drives the Windows
# Chrome). Idempotent — if a debug Chrome is already up on the port it just exits 0.
#
#   browse/chrome-attach.sh [--port N] [--profile DIR] [--browser PATH]
#
# Env equivalents: OPERATOR_CHROME_PORT, OPERATOR_CHROME_PROFILE, OPERATOR_CHROME_BIN
set -euo pipefail

PORT="${OPERATOR_CHROME_PORT:-9222}"
PROFILE="${OPERATOR_CHROME_PROFILE:-$HOME/.operator/chrome-profile}"
BIN="${OPERATOR_CHROME_BIN:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --browser) BIN="$2"; shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "$PROFILE"

# already up? (curl or wget; succeed quietly if the CDP port answers)
probe() { (command -v curl >/dev/null && curl -sf "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1) \
       || (command -v wget >/dev/null && wget -qO- "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1); }
if probe; then echo "chrome-attach: already running on :${PORT}"; exit 0; fi

# resolve a Chrome/Chromium binary per-OS unless one was given
uname_s="$(uname -s 2>/dev/null || echo unknown)"
is_wsl=0; grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null && is_wsl=1

if [ -z "$BIN" ]; then
  case "$uname_s" in
    Darwin)
      for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
               "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
        [ -x "$c" ] && BIN="$c" && break; done ;;
    Linux)
      if [ "$is_wsl" = 1 ]; then
        for c in "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe" \
                 "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"; do
          [ -f "$c" ] && BIN="$c" && break; done
      fi
      [ -z "$BIN" ] && for c in google-chrome google-chrome-stable chromium chromium-browser chrome; do
        command -v "$c" >/dev/null 2>&1 && BIN="$(command -v "$c")" && break; done ;;
    *)  # Git-Bash / MSYS on Windows
      for c in "/c/Program Files/Google/Chrome/Application/chrome.exe" \
               "/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"; do
        [ -f "$c" ] && BIN="$c" && break; done ;;
  esac
fi

if [ -z "$BIN" ]; then
  echo "chrome-attach: no Chrome/Chromium found. Install Chrome, or pass --browser /path/to/chrome." >&2
  exit 1
fi

echo "chrome-attach: launching $BIN on :${PORT} (profile: $PROFILE)"
FLAGS="--remote-debugging-port=${PORT} --user-data-dir=${PROFILE} --no-first-run --no-default-browser-check"

# WSL: hand the launch to Windows so Chrome parents under the Windows session
if [ "$is_wsl" = 1 ] && case "$BIN" in *.exe) true;; *) false;; esac; then
  win_profile="$(wslpath -w "$PROFILE" 2>/dev/null || echo "$PROFILE")"
  cmd.exe /c start "" "$BIN" --remote-debugging-port=${PORT} \
    --user-data-dir="$win_profile" --no-first-run --no-default-browser-check >/dev/null 2>&1 || true
else
  nohup "$BIN" $FLAGS >/dev/null 2>&1 &
fi

# wait briefly for the port to come up
for _ in $(seq 1 20); do probe && { echo "chrome-attach: up on :${PORT}"; exit 0; }; sleep 0.4; done
echo "chrome-attach: launched; CDP not yet answering on :${PORT} (it may need a moment)." >&2
exit 0
