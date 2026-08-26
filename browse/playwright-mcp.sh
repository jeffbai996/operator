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
  # v1.1: reserve one stable target for this conversation and expose only that
  # target plus tabs/popups it creates. Cookies and extensions remain shared;
  # another Operator conversation's pages do not.
  if [ "${OPERATOR_REQUIRE_CDP:-}" = "1" ] \
     && [ -n "${OPERATOR_CONVERSATION_ID:-}" ] \
     && [ -f "$HERE/operator_browser_tabs.py" ] \
     && [ -f "$HERE/operator_playwright_mcp.js" ]; then
    target="$(python3 "$HERE/operator_browser_tabs.py" reserve \
      "$OPERATOR_CONVERSATION_ID" "$EP" </dev/null 2>/dev/null || true)"
    if [ -z "$target" ]; then
      echo "playwright-mcp: FATAL — could not reserve a browser tab for conversation ${OPERATOR_CONVERSATION_ID}" >&2
      exit 1
    fi
    export OPERATOR_BROWSER_TARGET_ID="$target" OPERATOR_CDP_ENDPOINT="$EP"
    export OPERATOR_PLAYWRIGHT_NODE_MODULES="$HERE/node_modules"
    if command -v node >/dev/null 2>&1 \
       && [ -d "$HERE/node_modules/@playwright/mcp" ]; then
      exec node "$HERE/operator_playwright_mcp.js" | _gov
    fi
    echo "playwright-mcp: FATAL — run ./start.sh once to install the pinned browser bridge" >&2
    exit 1
  fi
  exec npx -y @playwright/mcp@latest --caps vision,pdf --output-dir "$OUT" --cdp-endpoint "$EP" | _gov
fi
# Cockpit runs (OPERATOR_REQUIRE_CDP=1, set by the launch adapters) must NEVER
# take the headless fallback: the live feed streams the CDP Chrome, so an agent
# in a fallback browser "works" invisibly while the visible browser sits dead.
# Fail the MCP loudly instead — the agent then reports the browser down.
if [ "${OPERATOR_REQUIRE_CDP:-}" = "1" ]; then
  echo "playwright-mcp: FATAL — no CDP endpoint on :${PORT} and OPERATOR_REQUIRE_CDP=1 forbids the headless fallback (run chrome-attach.sh)" >&2
  exit 1
fi
# no logged-in Chrome up → let the MCP launch its own (fresh) browser
exec npx -y @playwright/mcp@latest --caps vision,pdf --output-dir "$OUT" --viewport-size "$VIEWPORT" --headless | _gov
