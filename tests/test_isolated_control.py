"""The standalone runner must import only its explicitly staged artifact."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('isolated_control', ROOT / 'control/isolated_tests.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def test_control_import_closure_is_inside_the_artifact(tmp_path):
    M.stage(ROOT, 'control', tmp_path)
    result = subprocess.run([sys.executable, '-I', '-c',
        "import sys; sys.path.insert(0, '.'); import mcp_server,operator_job_tools,operator_workspace; "
        "from pathlib import Path; "
        "assert all(Path(m.__file__).resolve().parent == Path.cwd() for m in "
        "(mcp_server,operator_job_tools,operator_workspace))"],
        cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_flat_filename_collision_fails_before_execution(tmp_path):
    root = tmp_path / 'source'
    for part in ('control', 'vision', 'control/tests'):
        (root / part).mkdir(parents=True, exist_ok=True)
    (root / 'control/shared.py').write_text('')
    (root / 'vision/shared.py').write_text('')
    dest = tmp_path / 'artifact'
    dest.mkdir()
    with pytest.raises(ValueError, match='filename collision: shared.py'):
        M.stage(root, 'control', dest)
