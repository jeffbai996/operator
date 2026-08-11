#!/usr/bin/env bash
# Run the vision perception tests in isolation (avoids the operator package's
# flask import during pytest collection). Uses this module's own venv (numpy +
# pillow + pytesseract + pyyaml) — see requirements.txt.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/venv"
# Overridable so CI, which installs the deps into the job's own interpreter,
# does not need a venv at this path. The public mirror had this and the live
# copy did not, so mirroring live->public silently removed CI's only way to
# select a python (2026-08-11). Same shape control/run_tests.sh already uses.
PY="${VISION_TEST_PYTHON:-$VENV/bin/python3}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# copy the modules flat (NOT __init__.py — its relative imports only work when
# vision is imported as a package; the isolation run imports modules bare)
for f in "$HERE"/*.py; do
  [ "$(basename "$f")" = "__init__.py" ] && continue
  cp "$f" "$TMP/"
done
cp "$HERE"/tests/test_*.py "$TMP/"
cp -R "$HERE/maps" "$TMP/maps"
cd "$TMP"
"$PY" -m pytest -q "$@"
