"""Standalone control/vision test artifact with explicit dependency closure."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def stage(root: Path, suite: str, dest: Path) -> None:
    sources = list((root / suite).glob('*.py'))
    if suite == 'control':
        sources += list((root / 'vision').glob('*.py'))
        # Job state is part of the control MCP. Both imports are stdlib-only;
        # optional file-transfer dependencies are loaded only by that tool.
        sources += [root / name for name in ('operator_job_tools.py', 'operator_workspace.py')]
    sources += list((root / suite / 'tests').glob('test_*.py'))
    names = set()
    for source in sources:
        if source.name in {'__init__.py', 'isolated_tests.py'}:
            continue
        if source.name in names:
            raise ValueError(f'flat test artifact filename collision: {source.name}')
        names.add(source.name)
        shutil.copyfile(source, dest / source.name)
    shutil.copytree(root / 'vision/maps', dest / 'maps')


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    suite, *args = sys.argv[1:]
    if suite not in {'control', 'vision'}:
        raise ValueError('choose control or vision')
    with tempfile.TemporaryDirectory(prefix='operator-isolated-tests-') as temp:
        dest = Path(temp)
        stage(root, suite, dest)
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(('OPERATOR_', 'SQUAD_')) or key in {'PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS'}:
                env.pop(key, None)
        home = dest / 'home'
        home.mkdir()
        env.update(HOME=str(home), COMPUTER_USE_OUTPUT_DIR=str(dest / 'output'))
        return subprocess.call([sys.executable, '-m', 'pytest', '-q', *args], cwd=dest, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
