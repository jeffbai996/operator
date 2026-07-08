#!/usr/bin/env bash
# Run the control-layer tests in isolation (flat imports, no flask, no browser).
# Uses the vision venv by default (numpy + pillow + pytest + pyyaml cover these
# tests); override with CONTROL_TEST_PYTHON.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VISION="$HERE/../vision"
PY="${CONTROL_TEST_PYTHON:-$VISION/venv/bin/python3}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for f in "$HERE"/*.py "$VISION"/*.py; do
  [ "$(basename "$f")" = "__init__.py" ] && continue
  cp "$f" "$TMP/"
done
cp "$HERE"/tests/test_*.py "$TMP/"
cp -R "$VISION/maps" "$TMP/maps"
cd "$TMP"
"$PY" -m pytest -q "$@"
