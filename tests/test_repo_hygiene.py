"""Repository hygiene: OS metadata files stay out of version control."""

import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(_REPO), *args], capture_output=True, text=True)


pytestmark = pytest.mark.skipif(
    _git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true",
    reason="not running from a git checkout (e.g. an sdist)",
)


def test_no_ds_store_is_tracked():
    tracked = [f for f in _git("ls-files").stdout.splitlines() if f.endswith(".DS_Store")]
    assert tracked == []


def test_ds_store_is_ignored_everywhere():
    # check-ignore exits 0 when the path is ignored, in any subdirectory.
    assert _git("check-ignore", "-q", ".DS_Store").returncode == 0
    assert _git("check-ignore", "-q", "slurp/.DS_Store").returncode == 0


def test_versioned_build_directories_are_ignored():
    # Releases are built into dist-X.Y.Z/; a path that does not exist yet has no
    # .gitignore of uv's own beside it, so only the repository's rules apply.
    assert _git("check-ignore", "-q", "dist-9.9.9/slurp_graph-9.9.9.tar.gz").returncode == 0
    assert _git("check-ignore", "-q", "dist-9.9.9/").returncode == 0


def test_spreadsheets_stay_out_of_the_repository():
    # A roadmap spreadsheet sat untracked here and went into two published
    # sdists; spreadsheets are ignored wherever they are.
    assert _git("check-ignore", "-q", "Roadmap slurp.xlsx").returncode == 0
    assert _git("check-ignore", "-q", "docs/plan.xlsx").returncode == 0
    tracked = [f for f in _git("ls-files").stdout.splitlines() if f.endswith(".xlsx")]
    assert tracked == []
