"""Service environment variables cannot redirect tests to persistent state."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_bootstrap_overrides_inherited_store_locations(tmp_path):
    names = ["OPERATOR_" + kind + "_PATH"
             for kind in ("HISTORY", "SESSION", "STEER", "TASKS")]
    inherited = {name: str(tmp_path / (name + ".production")) for name in names}
    bootstrap = Path(__file__).with_name("conftest.py")
    code = ("import runpy,os,json; runpy.run_path(" + repr(str(bootstrap)) + ");"
            "print(json.dumps({k:os.environ[k] for k in " + repr(names) + "}))")
    result = subprocess.run([sys.executable, "-c", code],
                            env={**os.environ, **inherited},
                            capture_output=True, text=True, check=True)
    resolved = json.loads(result.stdout)
    assert all(resolved[name] != inherited[name] for name in names)
    assert len({str(Path(value).parent) for value in resolved.values()}) == 1
    assert not any(Path(value).exists() for value in inherited.values())
