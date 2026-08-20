"""Public release surfaces must move together.

The cockpit version used to advance while the README lockup, badge, status
copy, and screenshots stayed on an older release.  Keep the public-facing
bundle explicit so a future bump cannot silently strand any of them.
"""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
def test_public_release_assets_follow_operator_version() -> None:
    view = (ROOT / "operator_view.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    match = re.search(r'^OP_VERSION = "([^"]+)"$', view, re.MULTILINE)
    assert match is not None
    version = match.group(1)

    assert f"badge/version-{version}-" in readme
    assert f"**Status:** **v{version}**" in readme
    for theme in ("dark", "light"):
        name = f"operator-lockup-{theme}-v{version}.svg"
        assert name in readme
        lockup = (ROOT / "docs" / "img" / name).read_text(encoding="utf-8")
        assert f"Operator v{version}" in lockup
        assert f">v{version}</text>" in lockup

    showcases = re.findall(
        rf"docs/img/(operator-[^)\"']+-v{re.escape(version)}\.png)", readme)
    assert len(set(showcases)) >= 2
    for name in set(showcases):
        assert (ROOT / "docs" / "img" / name).is_file()
