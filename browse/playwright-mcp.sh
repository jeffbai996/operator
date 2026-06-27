#!/usr/bin/env bash
# playwright-mcp.sh — start Microsoft's Playwright MCP attached to the debug Chrome
# on :PORT (launched by chrome-attach.sh). Used as a stdio MCP command by the agent.
# Falls back to launching its own browser if no CDP endpoint is reachable.
#
# Env: OPERATOR_CHROME_PORT (default 9222), OPERATOR_VIEWPORT (default 1280,800),
#      OPERATOR_MCP_OUTPUT_DIR (default ~/.operator/screenshots)
set -euo pipefail
PORT="${OPERATOR_CHROME_PORT:-9222}"
VIEWPORT="${OPERATOR_VIEWPORT:-1280,800}"
OUT="${OPERATOR_MCP_OUTPUT_DIR:-$HOME/.operator/screenshots}"
mkdir -p "$OUT"

EP=""
if (command -v curl >/dev/null && curl -sf "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1); then
  EP="http://127.0.0.1:${PORT}"
fi

if [ -n "$EP" ]; then
  exec npx -y @playwright/mcp@latest --caps vision --output-dir "$OUT" --viewport-size "$VIEWPORT" --cdp-endpoint "$EP"
fi
# no logged-in Chrome up → let the MCP launch its own (fresh) browser
exec npx -y @playwright/mcp@latest --caps vision --output-dir "$OUT" --viewport-size "$VIEWPORT" --headless
