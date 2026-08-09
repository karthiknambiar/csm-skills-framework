"""Subprocess adapter for OfficeCLI document creation and updates."""

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from csaf.office.types import OfficeOperation, OfficeRenderRequest


class OfficeCLIError(RuntimeError):
    """Raised when OfficeCLI is unavailable or cannot render a document."""


@dataclass(frozen=True, slots=True)
class OfficeCLIConfig:
    """Configurable OfficeCLI invocation templates.

    Placeholders supported by argument templates are ``format``, ``spec``,
    ``output``, ``template``, and ``existing``.
    """

    executable: str = "officecli"
    create_arguments: tuple[str, ...] = (
        "create",
        "--format",
        "{format}",
        "--input",
        "{spec}",
        "--output",
        "{output}",
    )
    update_arguments: tuple[str, ...] = (
        "update",
        "--format",
        "{format}",
        "--input",
        "{spec}",
        "--existing",
        "{existing}",
        "--output",
        "{output}",
    )
    timeout_seconds: float = 120.0


class OfficeCLIArtifactRenderer:
    """Render Office files through a configurable OfficeCLI executable."""

    def __init__(self, config: OfficeCLIConfig | None = None) -> None:
        self._config = config or OfficeCLIConfig()

    def render(self, request: OfficeRenderRequest) -> bytes:
        """Write a temporary spec, invoke OfficeCLI, and return the output file."""

        if request.existing_path is not None and not request.existing_path.is_file():
            raise OfficeCLIError(f"existing Office artifact was not found: {request.existing_path}")
        if request.template_path is not None and not request.template_path.is_file():
            raise OfficeCLIError(f"Office template was not found: {request.template_path}")
        with tempfile.TemporaryDirectory(prefix="csaf-office-") as directory:
            working = Path(directory)
            spec = working / "document.json"
            suffix = ".pptx" if request.format.value == "powerpoint" else ".docx"
            output = working / f"artifact{suffix}"
            spec.write_text(request.model_dump_json(indent=2), encoding="utf-8")
            values = {
                "format": request.format.value,
                "spec": str(spec),
                "output": str(output),
                "template": str(request.template_path or ""),
                "existing": str(request.existing_path or ""),
            }
            template = (
                self._config.update_arguments
                if request.operation is OfficeOperation.UPDATE
                else self._config.create_arguments
            )
            arguments = [
                self._config.executable,
                *(argument.format_map(values) for argument in template),
            ]
            if request.template_path is not None:
                arguments.extend(("--template", str(request.template_path)))
            try:
                completed = subprocess.run(
                    arguments,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self._config.timeout_seconds,
                )
            except FileNotFoundError as error:
                raise OfficeCLIError(
                    f"OfficeCLI executable was not found: {self._config.executable}"
                ) from error
            except subprocess.TimeoutExpired as error:
                raise OfficeCLIError(
                    f"OfficeCLI exceeded the {self._config.timeout_seconds:g}s timeout"
                ) from error
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
                raise OfficeCLIError(
                    f"OfficeCLI failed with exit code {completed.returncode}: {detail}"
                )
            if not output.is_file():
                raise OfficeCLIError("OfficeCLI completed without creating the requested artifact")
            return output.read_bytes()
