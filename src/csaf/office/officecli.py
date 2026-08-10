"""Deterministic subprocess adapter for the local iOfficeAI/OfficeCLI."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from csaf.office.types import OfficeFormat, OfficeRenderRequest


class OfficeCLIError(RuntimeError):
    """Raised when OfficeCLI is unavailable or cannot render a document."""


@dataclass(frozen=True, slots=True)
class OfficeCLIConfig:
    """Configuration for the supported local OfficeCLI command surface."""

    executable: str = "officecli"
    prefix_arguments: tuple[str, ...] = ()
    timeout_seconds: float = 120.0
    minimum_version: tuple[int, int, int] = (1, 0, 137)


class OfficeCLIArtifactRenderer:
    """Render Office files through the selected local OfficeCLI executable."""

    def __init__(self, config: OfficeCLIConfig | None = None) -> None:
        self._config = config or OfficeCLIConfig()

    def render(self, request: OfficeRenderRequest) -> bytes:
        """Render a private working copy and return it only after validation."""

        source = self._source(request)
        self._version()
        with tempfile.TemporaryDirectory(prefix="csaf-office-") as directory:
            working = Path(directory)
            suffix = ".pptx" if request.format is OfficeFormat.POWERPOINT else ".docx"
            document = working / f"artifact{suffix}"
            batch = working / "batch.json"

            if source is None:
                self._run("create", str(document))
            else:
                shutil.copyfile(source, document)

            commands = (
                self._powerpoint_batch(request)
                if request.format is OfficeFormat.POWERPOINT
                else self._word_batch(request)
            )
            batch.write_text(
                json.dumps(commands, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            batch_result = self._json_envelope(
                "batch",
                self._run("batch", str(document), "--input", str(batch), "--json").stdout,
            )
            self._validate_batch_receipt(batch_result, len(commands))
            if not document.is_file():
                raise OfficeCLIError("OfficeCLI completed without creating the requested artifact")

            validation = self._json_envelope(
                "validation", self._run("validate", str(document), "--json").stdout
            )
            self._validate_validation(validation)

            issues = self._validate_issues(
                self._json_envelope(
                    "issue inspection",
                    self._run("view", str(document), "issues", "--json").stdout,
                )
            )
            fatal_issues = [
                issue for issue in issues if str(issue["Severity"]).casefold() == "error"
            ]
            if fatal_issues:
                detail = "; ".join(self._issue_detail(issue) for issue in fatal_issues)
                raise OfficeCLIError(f"OfficeCLI reported fatal document issues: {detail}")

            return document.read_bytes()

    def _source(self, request: OfficeRenderRequest) -> Path | None:
        if request.existing_path is not None:
            if not request.existing_path.is_file():
                raise OfficeCLIError(
                    f"existing Office artifact was not found: {request.existing_path}"
                )
            return request.existing_path
        if request.template_path is not None:
            if not request.template_path.is_file():
                raise OfficeCLIError(f"Office template was not found: {request.template_path}")
            return request.template_path
        return None

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = [
            self._config.executable,
            *self._config.prefix_arguments,
            *arguments,
        ]
        operation = arguments[0] if arguments else "command"
        environment = os.environ.copy()
        environment["OFFICECLI_RESIDENT_FLUSH"] = "each"
        try:
            with tempfile.TemporaryFile() as stdout_stream:
                with tempfile.TemporaryFile() as stderr_stream:
                    raw_completed = subprocess.run(
                        command,
                        check=False,
                        stdout=stdout_stream,
                        stderr=stderr_stream,
                        text=True,
                        encoding="utf-8",
                        errors="strict",
                        timeout=self._config.timeout_seconds,
                        env=environment,
                    )
                    stdout_stream.seek(0)
                    stderr_stream.seek(0)
                    stdout = stdout_stream.read().decode("utf-8", errors="strict")
                    stderr = stderr_stream.read().decode("utf-8", errors="strict")
        except FileNotFoundError as error:
            raise OfficeCLIError(
                f"OfficeCLI executable was not found: {self._config.executable}"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise OfficeCLIError(
                f"OfficeCLI {operation} exceeded the {self._config.timeout_seconds:g}s timeout"
            ) from error
        except UnicodeError as error:
            raise OfficeCLIError(
                f"OfficeCLI {operation} returned output that was not valid UTF-8"
            ) from error

        completed = subprocess.CompletedProcess(
            args=command,
            returncode=raw_completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise OfficeCLIError(
                f"OfficeCLI {operation} failed with exit code {completed.returncode}: {detail}"
            )
        return completed

    def _version(self) -> tuple[int, int, int]:
        output = self._run("--version").stdout
        match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", output)
        if match is None:
            raise OfficeCLIError(
                f"OfficeCLI returned an unrecognized version: {output.strip() or 'empty output'}"
            )
        version = tuple(int(part) for part in match.groups())
        if version < self._config.minimum_version:
            required = ".".join(str(part) for part in self._config.minimum_version)
            actual = ".".join(str(part) for part in version)
            raise OfficeCLIError(f"OfficeCLI {required} or newer is required; found {actual}")
        return version

    @staticmethod
    def _powerpoint_batch(request: OfficeRenderRequest) -> list[dict[str, Any]]:
        commands: list[dict[str, Any]] = [
            {
                "command": "add",
                "parent": "/",
                "type": "slide",
                "props": {"title": request.title, "layout": "title"},
            }
        ]
        if request.subtitle:
            commands.append(
                OfficeCLIArtifactRenderer._powerpoint_shape(
                    1,
                    request.subtitle,
                    y="9cm",
                    size="20",
                    color="666666",
                )
            )
        for slide_number, section in enumerate(request.sections, start=2):
            commands.append(
                {
                    "command": "add",
                    "parent": "/",
                    "type": "slide",
                    "props": {"title": section.title, "layout": "title"},
                }
            )
            for bullet_number, bullet in enumerate(section.bullets):
                commands.append(
                    OfficeCLIArtifactRenderer._powerpoint_shape(
                        slide_number,
                        f"\u2022 {bullet}",
                        y=f"{3 + bullet_number * 1.4:g}cm",
                        size="20",
                    )
                )
            if section.citations:
                commands.append(
                    OfficeCLIArtifactRenderer._powerpoint_shape(
                        slide_number,
                        f"Sources: {'; '.join(section.citations)}",
                        y="16.5cm",
                        size="10",
                        color="666666",
                    )
                )
        return commands

    @staticmethod
    def _powerpoint_shape(
        slide_number: int,
        text: str,
        *,
        y: str,
        size: str,
        color: str = "222222",
    ) -> dict[str, Any]:
        return {
            "command": "add",
            "parent": f"/slide[{slide_number}]",
            "type": "shape",
            "props": {
                "text": text,
                "x": "2cm",
                "y": y,
                "width": "20cm",
                "height": "1.2cm",
                "font": "Arial",
                "size": size,
                "bold": "false",
                "color": color,
                "fill": "none",
            },
        }

    @staticmethod
    def _word_batch(request: OfficeRenderRequest) -> list[dict[str, Any]]:
        commands = [OfficeCLIArtifactRenderer._word_paragraph(request.title, "Title")]
        if request.subtitle:
            commands.append(OfficeCLIArtifactRenderer._word_paragraph(request.subtitle, "Subtitle"))
        for section in request.sections:
            commands.append(OfficeCLIArtifactRenderer._word_paragraph(section.title, "Heading1"))
            commands.extend(
                OfficeCLIArtifactRenderer._word_paragraph(bullet, "ListBullet")
                for bullet in section.bullets
            )
            if section.citations:
                commands.append(
                    OfficeCLIArtifactRenderer._word_paragraph(
                        f"Sources: {'; '.join(section.citations)}",
                        "Caption",
                    )
                )
        return commands

    @staticmethod
    def _word_paragraph(text: str, style: str) -> dict[str, Any]:
        return {
            "command": "add",
            "parent": "/body",
            "type": "paragraph",
            "props": {"text": text, "style": style},
        }

    @staticmethod
    def _json_envelope(operation: str, output: str) -> Mapping[str, Any]:
        try:
            response = json.loads(output)
        except json.JSONDecodeError as error:
            raise OfficeCLIError(f"OfficeCLI {operation} returned invalid JSON") from error
        if not isinstance(response, Mapping):
            raise OfficeCLIError(f"OfficeCLI {operation} returned an unrecognized response")
        if response.get("success") is not True:
            detail = OfficeCLIArtifactRenderer._response_detail(response)
            raise OfficeCLIError(f"OfficeCLI {operation} failed: {detail}")
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise OfficeCLIError(f"OfficeCLI {operation} response is missing structured data")
        return data

    @staticmethod
    def _response_detail(response: Mapping[str, Any]) -> str:
        error = response.get("error")
        if isinstance(error, Mapping):
            for key in ("message", "description", "type"):
                value = error.get(key)
                if isinstance(value, str) and value:
                    return value
        if isinstance(error, str) and error:
            return error
        return "unsuccessful response"

    @staticmethod
    def _validate_batch_receipt(receipt: Mapping[str, Any], expected_total: int) -> None:
        summary = receipt.get("summary")
        results = receipt.get("results")
        if not isinstance(summary, Mapping) or not isinstance(results, list):
            raise OfficeCLIError("OfficeCLI batch returned an incomplete receipt")

        names = ("total", "executed", "succeeded", "failed", "skipped")
        counts: dict[str, int] = {}
        for name in names:
            value = summary.get(name)
            if type(value) is not int or value < 0:
                raise OfficeCLIError(f"OfficeCLI batch receipt has an invalid {name} count")
            counts[name] = value

        if "atomicRolledBack" in receipt:
            raise OfficeCLIError(
                "OfficeCLI batch receipt has an unexpected root atomicRolledBack value"
            )
        rolled_back = summary.get("atomicRolledBack", False)
        if type(rolled_back) is not bool:
            raise OfficeCLIError("OfficeCLI batch receipt has an invalid atomicRolledBack value")
        if rolled_back:
            raise OfficeCLIError("OfficeCLI batch was atomically rolled back")

        complete = (
            counts["total"] == expected_total
            and counts["executed"] == counts["total"]
            and counts["succeeded"] == counts["total"]
            and counts["failed"] == 0
            and counts["skipped"] == 0
            and counts["executed"] == counts["succeeded"] + counts["failed"]
            and counts["total"] == counts["executed"] + counts["skipped"]
            and len(results) == counts["total"]
        )
        if not complete:
            raise OfficeCLIError("OfficeCLI batch receipt is incomplete or inconsistent")

        indexes: list[int] = []
        for result in results:
            if (
                not isinstance(result, Mapping)
                or type(result.get("index")) is not int
                or result.get("success") is not True
            ):
                raise OfficeCLIError("OfficeCLI batch receipt contains an unsuccessful result")
            indexes.append(result["index"])
        if sorted(indexes) != list(range(expected_total)):
            raise OfficeCLIError("OfficeCLI batch receipt contains incomplete result indexes")

    @staticmethod
    def _validate_validation(validation: Mapping[str, Any]) -> None:
        count = validation.get("count")
        errors = validation.get("errors")
        if type(count) is not int or count < 0 or not isinstance(errors, list):
            raise OfficeCLIError("OfficeCLI validation returned an unrecognized result")
        if count != len(errors):
            raise OfficeCLIError("OfficeCLI validation returned an inconsistent error count")

        details: list[str] = []
        for error in errors:
            if not isinstance(error, Mapping):
                raise OfficeCLIError("OfficeCLI validation returned a malformed error")
            required = ("type", "description", "path", "part")
            if any(key not in error for key in required):
                raise OfficeCLIError("OfficeCLI validation returned a malformed error")
            if not all(isinstance(error[key], str) or error[key] is None for key in required):
                raise OfficeCLIError("OfficeCLI validation returned a malformed error")
            details.append(
                " | ".join(str(error[key]) for key in required if error[key] not in (None, ""))
            )
        if details:
            raise OfficeCLIError(f"OfficeCLI validation failed: {'; '.join(details)}")

    @staticmethod
    def _validate_issues(
        issue_data: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        count = issue_data.get("Count")
        issues = issue_data.get("Issues")
        if type(count) is not int or count < 0 or not isinstance(issues, list):
            raise OfficeCLIError("OfficeCLI issue inspection returned an unrecognized result")
        if count != len(issues):
            raise OfficeCLIError("OfficeCLI issue inspection returned an inconsistent issue count")

        structured: list[Mapping[str, Any]] = []
        for issue in issues:
            if not isinstance(issue, Mapping):
                raise OfficeCLIError("OfficeCLI issue inspection returned a malformed issue")
            if any(key not in issue for key in ("Severity", "Message", "Path")):
                raise OfficeCLIError("OfficeCLI issue inspection returned a malformed issue")
            if (
                not isinstance(issue["Severity"], str)
                or not issue["Severity"]
                or not isinstance(issue["Message"], str)
                or not (isinstance(issue["Path"], str) or issue["Path"] is None)
            ):
                raise OfficeCLIError("OfficeCLI issue inspection returned a malformed issue")
            structured.append(issue)
        return structured

    @staticmethod
    def _issue_detail(issue: Mapping[str, Any]) -> str:
        return " | ".join(
            str(issue[key])
            for key in ("Severity", "Message", "Path")
            if issue[key] not in (None, "")
        )
