#!/usr/bin/env bash
# playwright-mcp.sh — start Microsoft's Playwright MCP attached to the debug Chrome
# on :PORT (launched by chrome-attach.sh). Used as a stdio MCP command by the agent.
# Falls back to launching its own browser if no CDP endpoint is reachable.
#
# Env: OPERATOR_CHROME_PORT (default 9222), OPERATOR_VIEWPORT (default 1280,800),
#      OPERATOR_MCP_OUTPUT_DIR (default ~/.operator/screenshots)
#      OPERATOR_DEMO_CDP (explicit CDP endpoint override; skips the auto-probe)
set -euo pipefail
PORT="${OPERATOR_CHROME_PORT:-9222}"
VIEWPORT="${OPERATOR_VIEWPORT:-1280,800}"
OUT="${OPERATOR_MCP_OUTPUT_DIR:-$HOME/.operator/screenshots}"
mkdir -p "$OUT"

EP=""
# Explicit endpoint override (e.g. an isolated demo Chrome): attach straight to it,
# skip the auto-probe. Unset for normal use → original behavior.
if [ -n "${OPERATOR_DEMO_CDP:-}" ]; then
  EP="$OPERATOR_DEMO_CDP"
elif (command -v curl >/dev/null && curl -sf "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1); then
  EP="http://127.0.0.1:${PORT}"
fi

# ── image governor: downscale oversized screenshot blocks on the server→client
# side of the pipe before the model ingests them — accumulated screenshots
# re-sent every turn are the dominant vision-task token cost. Fail-open: the
# script passes bytes through untouched when sharp isn't installed, and we fall
# back to plain exec if it's absent. Knobs: OPERATOR_IMG_MAX_EDGE (0 disables),
# OPERATOR_IMG_JPEG_Q.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOV="$HERE/mcp_image_governor.js"
_gov() {
  if [ -f "$GOV" ] && command -v node >/dev/null 2>&1; then exec node "$GOV"; else exec cat; fi
}

if [ -n "$EP" ]; then
  exec npx -y @playwright/mcp@latest --caps vision,pdf --output-dir "$OUT" --cdp-endpoint "$EP" | _gov
fi
# no logged-in Chrome up → let the MCP launch its own (fresh) browser
exec npx -y @playwright/mcp@latest --caps vision,pdf --output-dir "$OUT" --viewport-size "$VIEWPORT" --headless | _gov
