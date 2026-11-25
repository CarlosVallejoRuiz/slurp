"""What a release ships: the package and its docs, nothing else from the tree.

The sdists of 1.0.7 and 1.1.0 were built in a working tree holding an
untracked spreadsheet, and hatchling put it in the tarball: anything not
ignored by git went in. The sdist is now a whitelist.
"""

import tomllib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO / ".github" / "workflows" / "publish.yml"


def _sdist_config() -> dict:
    data = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["sdist"]


def test_the_sdist_is_a_whitelist_of_the_package_and_its_docs():
    # hatchling always adds pyproject.toml, PKG-INFO and .gitignore.
    assert _sdist_config()["only-include"] == ["slurp", "README.md", "USAGE.md"]


def test_the_wheel_ships_the_package_alone():
    data = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["slurp"]


@pytest.mark.skipif(not _WORKFLOW.exists(), reason="no .github/ (e.g. an sdist)")
def test_the_release_build_refuses_an_sdist_with_anything_else():
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "tar -tzf" in text and "unexpected file in the sdist" in text
