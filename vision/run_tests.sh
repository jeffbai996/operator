#!/usr/bin/env bash
# Source tests use repository admission; standalone tests use a closed artifact.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
PY="${VISION_TEST_PYTHON:-python3}"
if [ -f "$ROOT/scripts/cc_test.py" ]; then
  exec "$PY" "$ROOT/scripts/cc_test.py" --worktree "$ROOT" --test "$HERE/tests" "$@"
fi
exec "$PY" "$HERE/../control/isolated_tests.py" vision "$@"
