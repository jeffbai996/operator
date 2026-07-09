#!/usr/bin/env bash
# One-command start: venv + deps + the automation Chrome + the app.
# Idempotent — safe to re-run any time; it skips whatever is already in place.
set -euo pipefail
cd "$(dirname "$0")"

PY="${OPERATOR_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python
[ -d venv ] || { echo "── creating venv ──"; "$PY" -m venv venv; }
./venv/bin/pip install -q -r requirements.txt

# Agent runtimes (all optional — the cockpit runs without any; you can still
# watch + steer the browser manually). Install one and log in to hand it the wheel.
echo "── agent runtimes ──"
found_any=0
while IFS=: read -r bin name hint; do
  if command -v "$bin" >/dev/null 2>&1; then echo "  ✓ $name"; found_any=1
  else echo "  · $name — $hint"; fi
done <<'RUNTIMES'
claude:Claude (Claude Code):npm i -g @anthropic-ai/claude-code && claude login
codex:GPT (Codex CLI):npm i -g @openai/codex && codex login
agy:Gemini (Antigravity):install agy + sign in with a Google account
RUNTIMES
[ "$found_any" = 1 ] || echo "  (none found — manual mode only until you install one)"

# Docker → the isolated sandbox desktop surface (optional)
if command -v docker >/dev/null 2>&1; then echo "  ✓ Docker — sandbox desktop available"
else echo "  · Docker not found — sandbox desktop surface stays off"; fi

# The Chrome the agent drives — launch only if CDP isn't already up.
if ! curl -sf --max-time 2 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
  echo "── launching the automation Chrome (sign into your sites there, once) ──"
  bash browse/chrome-attach.sh \
    || echo "  (Chrome didn't launch — retry later with: bash browse/chrome-attach.sh)"
fi

echo "── Operator → http://127.0.0.1:${OPERATOR_PORT:-5005} ──"
exec ./venv/bin/python app.py
