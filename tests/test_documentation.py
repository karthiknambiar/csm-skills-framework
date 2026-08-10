"""Checks that documentation links and runnable examples remain valid."""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")
_RELEASE_TEXT_FILES = (
    Path("README.md"),
    Path("CONTRIBUTING.md"),
    Path("docs/officecli.md"),
    Path("docs/rest-api.md"),
    Path("docs/compatibility.md"),
)


def test_relative_markdown_links_resolve() -> None:
    markdown_files = [Path("README.md"), Path("CONTRIBUTING.md")]
    markdown_files.extend(sorted(Path("docs").glob("*.md")))
    markdown_files.extend(sorted(Path("examples").glob("*.md")))
    failures: list[str] = []
    for document in markdown_files:
        for target in _MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#"):
                continue
            relative = target.split("#", maxsplit=1)[0]
            if relative and not (document.parent / relative).resolve().exists():
                failures.append(f"{document}: {target}")
    assert failures == []


def test_release_documentation_is_clean_utf8() -> None:
    failures: list[str] = []
    for document in _RELEASE_TEXT_FILES:
        raw = document.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            failures.append(f"{document}: UTF-8 BOM")
        text = raw.decode("utf-8")
        for marker in ("\u00e2\u201d", "\u00c3", "\ufffd"):
            if marker in text:
                failures.append(f"{document}: mojibake marker {marker!r}")
    assert failures == []


@pytest.mark.parametrize(
    "script",
    [
        "examples/account_brief.py",
        "examples/meeting_copilot.py",
        "examples/ingest_json.py",
    ],
)
def test_python_example_runs(script: str) -> None:
    completed = subprocess.run(
        [sys.executable, script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip()


def test_release_metadata_matches_repository_and_build_tooling() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]

    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE"]
    assert project["urls"] == {
        "Documentation": "https://github.com/karthiknambiar/csm-skills-framework#readme",
        "Source": "https://github.com/karthiknambiar/csm-skills-framework",
    }
    dev_dependencies = set(project["optional-dependencies"]["dev"])
    assert "build>=1.2,<2" in dev_dependencies
    assert "httpx2>=2,<3" in dev_dependencies
    assert not any(dependency.startswith("httpx>=") for dependency in dev_dependencies)


def test_repository_contains_canonical_apache_license() -> None:
    license_text = Path("LICENSE").read_text(encoding="utf-8")

    assert license_text.lstrip().startswith(
        "Apache License\n                           Version 2.0, January 2004"
    )
    assert "http://www.apache.org/licenses/" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert "Copyright {yyyy} {name of copyright owner}" in license_text


def test_officecli_documentation_describes_supported_local_runtime() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    guide = Path("docs/officecli.md").read_text(encoding="utf-8")
    combined = f"{readme}\n{guide}"

    for required in (
        "iOfficeAI/OfficeCLI",
        "1.0.137",
        "csaf office doctor",
        "csaf office doctor --json",
        "irm https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.ps1 | iex",
        "curl -fsSL https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.sh | sh",
        "officecli create",
        "officecli batch",
        "officecli validate",
        "officecli view",
    ):
        assert required in combined

    lowered = combined.lower()
    assert "fully local" in lowered
    assert "deterministic" in lowered
    assert "never installs" in lowered
    assert "api key" in lowered
    assert "hosted model" in lowered


def test_security_and_migration_documentation_matches_public_contracts() -> None:
    rest = Path("docs/rest-api.md").read_text(encoding="utf-8").lower()
    compatibility = Path("docs/compatibility.md").read_text(encoding="utf-8").lower()
    contributing = Path("CONTRIBUTING.md").read_text(encoding="utf-8").lower()

    assert "host application" in rest
    assert "authentication" in rest and "authorization" in rest
    assert "do not expose" in rest
    assert "no built-in api authentication" in rest

    assert "action_item" in compatibility
    assert "commitment" in compatibility
    assert "argument-template" in compatibility
    assert "officeartifactrenderer" in compatibility

    assert 'uv pip install --python .\\.venv\\scripts\\python.exe -e ".[dev]"' in contributing
    assert "python scripts/check_secrets.py --worktree --tracked --history" in contributing
    assert "python -m build" in contributing
