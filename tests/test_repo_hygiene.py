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
