"""Checks that documentation links and runnable examples remain valid."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

_MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


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
