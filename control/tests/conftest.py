"""Allow bounded repository tests to collect the flat game harness modules."""
import sys
from pathlib import Path

_operator = Path(__file__).resolve().parents[2]
for _part in ("control", "vision"):
    sys.path.insert(0, str(_operator / _part))
