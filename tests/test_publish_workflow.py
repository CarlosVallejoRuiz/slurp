"""The PyPI publish workflow keeps its security properties.

Read as text: PyYAML is not a dependency, and these properties are visible in
the file as written.
"""

import re
from pathlib import Path

import pytest

_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "publish.yml"

pytestmark = pytest.mark.skipif(not _WORKFLOW.exists(), reason="no .github/ (e.g. an sdist)")


def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _job(name: str) -> str:
    """The block of one job, from its key to the next job key."""
    text = _text()
    start = text.index(f"\n  {name}:\n")
    nxt = re.search(r"\n  [a-z][\w-]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def _run_blocks(text: str) -> list[str]:
    """Every `run:` script, inline or block."""
    blocks, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", line)
        if not m:
            continue
        if m.group(2) not in ("|", ">", "|-", ">-"):
            blocks.append(m.group(2))
            continue
        indent, body = len(m.group(1)), []
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt)
        blocks.append("\n".join(body))
    return blocks


def test_every_action_is_pinned_to_a_commit_with_its_release_named():
    uses = re.findall(r"uses:\s*(\S+)(.*)", _text())
    assert uses
    for ref, comment in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
        assert re.fullmatch(r"\s*# v\d+\.\d+\.\d+", comment), f"{ref}{comment}"


def test_runs_on_release_tags_and_on_demand_for_an_existing_tag():
    text = _text()
    assert re.search(r'push:\s*\n\s*tags:\s*\["v\*"\]', text)
    dispatch = text[text.index("workflow_dispatch:"):text.index("permissions:")]
    assert re.search(r"tag:\s*\n(\s+\w.*\n)*?\s+required: true", dispatch)


def test_a_tag_push_builds_but_never_publishes_or_deploys():
    # Until PyPI's trusted publisher is configured, publishing on every tag
    # failed with invalid-publisher and left a failed deployment in `pypi`
    # (v1.1.0 to v1.4.0). A tag push runs build only; publish runs by hand.
    publish, build = _job("publish"), _job("build")
    job_level_if = re.compile(r"^    if:\s*(.+?)\s*$", re.M)
    assert job_level_if.findall(publish) == ["github.event_name == 'workflow_dispatch'"]
    assert job_level_if.findall(build) == []
    # Only a job in an environment creates a deployment, and only publish has one.
    assert _text().count("environment:") == 1
    assert "environment:" in publish


def test_only_the_publish_job_can_mint_an_oidc_token():
    assert _text().count("id-token: write") == 1
    publish = _job("publish")
    assert "id-token: write" in publish
    assert re.search(r"environment:\s*\n\s*name: pypi", publish)
    assert "needs: build" in publish
    assert "pypa/gh-action-pypi-publish@" in publish
    assert "id-token" not in _job("build")


def test_no_secret_or_token_is_used():
    text = _text()
    assert "secrets." not in text
    assert not re.search(r"^\s*(password|api-token|token):", text, re.M)


def test_build_checks_versions_runs_the_suite_with_every_extra_and_keeps_dist():
    build = _job("build")
    assert "refs/tags/" in build
    assert "pyproject.toml" in build and "slurp/__init__.py" in build
    assert "uv sync --locked --all-extras" in build
    assert "pytest tests/" in build
    assert "uv build" in build
    assert "actions/upload-artifact@" in build and "name: dist" in build


def test_the_tag_never_reaches_a_shell_through_an_expression():
    # ${{ }} inside a script is spliced in before the shell parses it: a tag
    # name could inject commands. Scripts read it from the TAG variable.
    for script in _run_blocks(_text()):
        assert "${{" not in script, script
