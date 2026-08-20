"""Smoke tests for the installable project scaffold."""

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import csaf

ROOT = Path(__file__).resolve().parents[1]
QBR_TEMPLATES = ROOT / "src" / "csaf" / "templates" / "qbr"


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


def test_package_exposes_version() -> None:
    assert csaf.__version__ == "0.1.0.dev0"


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
