"""Smoke tests for the installable project scaffold."""

import ast
import json
import subprocess
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest

import csaf

ROOT = Path(__file__).resolve().parents[1]
QBR_TEMPLATES = ROOT / "src" / "csaf" / "templates" / "qbr"
PROJECT_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "version"
]


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build one isolated wheel for package-byte assertions."""

    output = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    wheels = list(output.glob("csaf-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_package_exposes_project_version() -> None:
    assert csaf.__version__ == PROJECT_VERSION


def test_wheel_runtime_version_matches_metadata_and_project(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as wheel:
        metadata_path = f"csaf-{PROJECT_VERSION}.dist-info/METADATA"
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
        runtime_module = ast.parse(wheel.read("csaf/_version.py"))

    assignments = [
        statement
        for statement in runtime_module.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        )
    ]
    assert len(assignments) == 1
    runtime_version = ast.literal_eval(assignments[0].value)
    assert runtime_version == metadata["Version"] == PROJECT_VERSION


def test_wheel_contains_exact_vetted_qbr_template_bytes(built_wheel: Path) -> None:
    """A clean wheel contains exact source assets and canonical provenance bytes."""

    prefix = "csaf/templates/qbr/"
    expected = ("default-qbr.pptx", "default-qbr.docx", "provenance.json")
    with zipfile.ZipFile(built_wheel) as wheel:
        names = set(wheel.namelist())
        for name in expected:
            assert prefix + name in names
            assert wheel.read(prefix + name) == (QBR_TEMPLATES / name).read_bytes()
        provenance = json.loads(wheel.read(prefix + "provenance.json"))
        assert provenance["source_commit"] == "459b1a473faf33f2f52e697ac6d265a3f67b176a"
        assert provenance["license"] == "Apache-2.0"
