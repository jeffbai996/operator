#!/usr/bin/env bash
# operator-mcp.sh — launch the operator control MCP (perceive / game_macro /
# desktop computer actions) as a stdio server for a headless agent run.
#
# Registered per-run by operator_agent.py alongside the Playwright MCP. The
# active surface arrives in OPERATOR_SURFACE (browser | desktop-sandbox |
# desktop-real); the bot name for the trace in OPERATOR_BOT.
#
# Python resolution: control/venv if present (playwright + numpy + pillow +
# pytesseract + pyyaml — see requirements.txt), else the vision venv (no
# playwright → desktop surfaces still work; browser surface will error on
# first use with a clear message), else system python3. Vision + control both
# go on PYTHONPATH — the server imports its modules flat.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VISION="$(cd "$HERE/../vision" && pwd)"

PY="${OPERATOR_MCP_PYTHON:-}"
if [ -z "$PY" ] && [ -x "$HERE/venv/bin/python3" ]; then PY="$HERE/venv/bin/python3"; fi
if [ -z "$PY" ] && [ -x "$VISION/venv/bin/python3" ]; then PY="$VISION/venv/bin/python3"; fi
if [ -z "$PY" ]; then PY="$(command -v python3)"; fi

export PYTHONPATH="$HERE:$VISION${PYTHONPATH:+:$PYTHONPATH}"
# WSL interop dir for powershell.exe (desktop-real backend) — absent from PATH
# under systemd --user units; harmless elsewhere.
[ -d /mnt/c/Windows/System32/WindowsPowerShell/v1.0 ] && \
  export PATH="$PATH:/mnt/c/Windows/System32/WindowsPowerShell/v1.0"
exec "$PY" "$HERE/mcp_server.py"
