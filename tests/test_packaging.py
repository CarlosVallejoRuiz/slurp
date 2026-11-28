"""What a release ships: the package and its docs, nothing else from the tree.

The sdists of 1.0.7 and 1.1.0 were built in a working tree holding an
untracked spreadsheet, and hatchling put it in the tarball: anything not
ignored by git went in. The sdist is now a whitelist.
"""

import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO / ".github" / "workflows" / "publish.yml"


def _sdist_config() -> dict:
    data = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["sdist"]


def _project() -> dict:
    return tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_the_sdist_is_a_whitelist_of_the_package_its_docs_and_license():
    # hatchling always adds pyproject.toml, PKG-INFO and .gitignore.
    assert _sdist_config()["only-include"] == ["slurp", "README.md", "USAGE.md", "LICENSE"]


def test_license_is_the_mit_text_and_declared_as_the_license_file():
    text = (_REPO / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Juan Carlos Vallejo Ruiz" in text
    assert "THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND" in text
    assert _project()["license"] == "MIT"
    assert _project()["license-files"] == ["LICENSE"]
    # PEP 639: an SPDX expression supersedes the license classifiers, and
    # uploads that carry both can be refused.
    assert not [c for c in _project()["classifiers"] if c.startswith("License ::")]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_a_real_build_ships_the_license_in_both_files_and_nothing_private(tmp_path):
    # Builds exactly as a release does: the sdist, then the wheel from it.
    res = subprocess.run(["uv", "build", "--out-dir", str(tmp_path), str(_REPO)],
                         capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    version = _project()["version"]
    with tarfile.open(tmp_path / f"slurp_graph-{version}.tar.gz") as sdist:
        names = {n.removeprefix(f"slurp_graph-{version}/") for n in sdist.getnames()}
    assert "LICENSE" in names
    assert not {n for n in names if n.endswith(".xlsx") or n == "CLAUDE.md"
                or n.startswith(".github")}
    with zipfile.ZipFile(tmp_path / f"slurp_graph-{version}-py3-none-any.whl") as wheel:
        assert f"slurp_graph-{version}.dist-info/licenses/LICENSE" in wheel.namelist()
        metadata = wheel.read(f"slurp_graph-{version}.dist-info/METADATA").decode()
    assert "License-Expression: MIT" in metadata and "License-File: LICENSE" in metadata


def test_the_wheel_ships_the_package_alone():
    data = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["slurp"]


@pytest.mark.skipif(not _WORKFLOW.exists(), reason="no .github/ (e.g. an sdist)")
def test_the_release_build_refuses_an_sdist_with_anything_else():
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "tar -tzf" in text and "unexpected file in the sdist" in text
    assert "|LICENSE|" in text  # the guard lets the license through


# ---------------------------------------------------------------------------
# Dependencies (v1.5, #23 and #44)
# ---------------------------------------------------------------------------

def _requirement_names(specs: list[str]) -> set[str]:
    import re
    return {re.split(r"[<>=!~;\[ ]", s, maxsplit=1)[0].strip().lower() for s in specs}


def test_anthropic_is_an_extra_like_openai_not_a_dependency():
    # Only `slurp explain` with the anthropic provider needs it; everything else
    # (index, query, structural explain, impact, the MCP server) runs without.
    project = _project()
    assert "anthropic" not in _requirement_names(project["dependencies"])
    extras = project["optional-dependencies"]
    assert _requirement_names(extras["llm-anthropic"]) == {"anthropic"}
    assert _requirement_names(extras["llm-openai"]) == {"openai"}


def test_pathspec_is_gone_nothing_imports_it():
    assert "pathspec" not in _requirement_names(_project()["dependencies"])
    for path in (_REPO / "slurp").rglob("*.py"):
        assert "pathspec" not in path.read_text(encoding="utf-8"), path


def test_every_module_imports_without_the_llm_packages():
    # A clean install has neither anthropic nor openai: no module may import
    # them at load time (they are imported where a provider is called).
    import sys
    modules = sorted(p.stem for p in (_REPO / "slurp").glob("*.py") if p.stem != "__main__")
    code = ("import sys; sys.modules['anthropic'] = None; sys.modules['openai'] = None\n"
            + "".join(f"import slurp.{m}\n" for m in modules))
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=_REPO, check=False)
    assert res.returncode == 0, res.stderr


_WEBSITE = "https://imjuank.dev/slurp-web/"


def test_the_readme_links_the_website_at_the_top():
    top = (_REPO / "README.md").read_text(encoding="utf-8").splitlines()[:16]
    assert any(f"]({_WEBSITE})" in line for line in top)

