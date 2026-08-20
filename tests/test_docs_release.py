"""Public release surfaces must move together.

The cockpit version used to advance while the README lockup, badge, status
copy, and screenshots stayed on an older release.  Keep the public-facing
bundle explicit so a future bump cannot silently strand any of them.
"""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = "1.0.37"


def test_public_release_surfaces_match_current_version() -> None:
    view = (ROOT / "operator_view.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    match = re.search(r'^OP_VERSION = "([^"]+)"$', view, re.MULTILINE)
    assert match is not None
    assert match.group(1) == CURRENT_VERSION

    assert f"badge/version-{CURRENT_VERSION}-" in readme
    assert f"**Status:** **v{CURRENT_VERSION}**" in readme
    for theme in ("dark", "light"):
        name = f"operator-lockup-{theme}-v{CURRENT_VERSION}.svg"
        assert name in readme
        lockup = (ROOT / "docs" / "img" / name).read_text(encoding="utf-8")
        assert f"Operator v{CURRENT_VERSION}" in lockup
        assert f">v{CURRENT_VERSION}</text>" in lockup


def test_readme_uses_the_current_showcase_pair() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assets = (
        "operator-trip-planning-v1.0.37.png",
        "operator-live-research-v1.0.37.png",
    )

    for name in assets:
        assert f"docs/img/{name}" in readme
        assert (ROOT / "docs" / "img" / name).is_file()
