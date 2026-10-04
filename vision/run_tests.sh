#!/usr/bin/env bash
# Source tests use repository admission; standalone tests use a closed artifact.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
if [ -f "$ROOT/scripts/cc_test.py" ]; then
  PY="${VISION_TEST_PYTHON:-python3}"
  exec "$PY" "$ROOT/scripts/cc_test.py" --worktree "$ROOT" --test "$HERE/tests" "$@"
fi
PY="${VISION_TEST_PYTHON:-$HERE/venv/bin/python3}"
exec "$PY" "$HERE/../control/isolated_tests.py" vision "$@"
