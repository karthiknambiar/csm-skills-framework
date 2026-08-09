"""Tests for the configurable OfficeCLI subprocess boundary."""

import sys
from pathlib import Path

import pytest

from csaf.office import (
    OfficeCLIArtifactRenderer,
    OfficeCLIConfig,
    OfficeCLIError,
    OfficeFormat,
    OfficeRenderRequest,
    OfficeSection,
)


def test_officecli_adapter_passes_spec_and_returns_output(tmp_path: Path) -> None:
    bridge = tmp_path / "fake_officecli.py"
    bridge.write_text(
        """
import json
import pathlib
import sys

arguments = dict(zip(sys.argv[1::2], sys.argv[2::2]))
spec = json.loads(pathlib.Path(arguments["--input"]).read_text())
pathlib.Path(arguments["--output"]).write_bytes(("rendered:" + spec["title"]).encode())
""".strip()
    )
    renderer = OfficeCLIArtifactRenderer(
        OfficeCLIConfig(
            executable=sys.executable,
            create_arguments=(
                str(bridge),
                "--input",
                "{spec}",
                "--output",
                "{output}",
            ),
        )
    )

    content = renderer.render(
        OfficeRenderRequest(
            format=OfficeFormat.POWERPOINT,
            title="Acme QBR",
            sections=(OfficeSection(title="Summary", bullets=("On track",)),),
        )
    )

    assert content == b"rendered:Acme QBR"


def test_officecli_adapter_reports_missing_executable() -> None:
    renderer = OfficeCLIArtifactRenderer(
        OfficeCLIConfig(executable="officecli-command-that-does-not-exist")
    )

    with pytest.raises(OfficeCLIError, match="was not found"):
        renderer.render(
            OfficeRenderRequest(
                format=OfficeFormat.WORD,
                title="QBR",
                sections=(OfficeSection(title="Summary"),),
            )
        )


def test_officecli_adapter_rejects_missing_update_source(tmp_path: Path) -> None:
    renderer = OfficeCLIArtifactRenderer()

    with pytest.raises(OfficeCLIError, match="existing Office artifact was not found"):
        renderer.render(
            OfficeRenderRequest(
                format=OfficeFormat.WORD,
                operation="update",
                title="QBR",
                sections=(OfficeSection(title="Summary"),),
                existing_path=tmp_path / "missing.docx",
            )
        )
