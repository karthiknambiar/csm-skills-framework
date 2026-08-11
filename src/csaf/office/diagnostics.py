"""Local OfficeCLI readiness diagnostics for deterministic QBR rendering."""

import shutil
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from csaf.office.officecli import (
    OfficeCLIArtifactRenderer,
    OfficeCLIConfig,
    OfficeCLIError,
)
from csaf.office.redaction import redact_officecli_message
from csaf.office.types import OfficeFormat, OfficeRenderRequest, OfficeSection


class DiagnosticStatus(StrEnum):
    """Outcome of one readiness diagnostic."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class DiagnosticCheck(BaseModel):
    """One deterministic OfficeCLI readiness check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: DiagnosticStatus
    message: str


class OfficeDiagnosticReport(BaseModel):
    """Complete readiness report for local QBR generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ready: bool
    checks: tuple[DiagnosticCheck, ...]


class OfficeCLIDoctor:
    """Check OfficeCLI without reading or changing user documents."""

    _CHECK_NAMES = ("executable", "version", "powerpoint-smoke", "word-smoke")

    def __init__(
        self,
        source: OfficeCLIConfig | OfficeCLIArtifactRenderer | None = None,
    ) -> None:
        if isinstance(source, OfficeCLIArtifactRenderer):
            self._renderer = source
            self._config = source._config
        else:
            self._config = source or OfficeCLIConfig()
            self._renderer = OfficeCLIArtifactRenderer(self._config)

    def preflight(self) -> None:
        """Fail quickly unless the executable and selected version are ready."""

        executable = self._check_executable()
        if executable.status is DiagnosticStatus.FAIL:
            raise OfficeCLIError(executable.message)
        try:
            self._renderer._version()
        except (OSError, OfficeCLIError) as error:
            raise OfficeCLIError(redact_officecli_message(str(error))) from error

    def run(self) -> OfficeDiagnosticReport:
        """Run ordered executable, version, PPTX, and DOCX checks."""

        checks: list[DiagnosticCheck] = [self._check_executable()]
        if checks[0].status is DiagnosticStatus.FAIL:
            checks.extend(self._skipped(self._CHECK_NAMES[1:], "executable check failed"))
            return OfficeDiagnosticReport(ready=False, checks=tuple(checks))

        try:
            version = self._renderer._version()
        except (OSError, OfficeCLIError) as error:
            checks.append(self._failed("version", error))
            checks.extend(self._skipped(self._CHECK_NAMES[2:], "version check failed"))
            return OfficeDiagnosticReport(ready=False, checks=tuple(checks))
        checks.append(
            DiagnosticCheck(
                name="version",
                status=DiagnosticStatus.PASS,
                message=f"OfficeCLI {'.'.join(str(part) for part in version)} is supported",
            )
        )

        checks.append(self._smoke(OfficeFormat.POWERPOINT, "powerpoint-smoke"))
        checks.append(self._smoke(OfficeFormat.WORD, "word-smoke"))
        return OfficeDiagnosticReport(
            ready=all(check.status is DiagnosticStatus.PASS for check in checks),
            checks=tuple(checks),
        )

    def _check_executable(self) -> DiagnosticCheck:
        executable = self._config.executable
        resolved = shutil.which(executable)
        if resolved is None and Path(executable).is_file():
            resolved = str(Path(executable).resolve())
        if resolved is None:
            return DiagnosticCheck(
                name="executable",
                status=DiagnosticStatus.FAIL,
                message=redact_officecli_message(
                    f"OfficeCLI executable was not found: {executable}"
                ),
            )
        return DiagnosticCheck(
            name="executable",
            status=DiagnosticStatus.PASS,
            message="OfficeCLI executable is available",
        )

    def _smoke(self, format: OfficeFormat, name: str) -> DiagnosticCheck:
        try:
            content = self._renderer.render(
                OfficeRenderRequest(
                    format=format,
                    title="CSAF OfficeCLI readiness check",
                    sections=(OfficeSection(title="Smoke test", bullets=("Ready",)),),
                )
            )
            if not content:
                raise OfficeCLIError("OfficeCLI returned an empty artifact")
        except (OSError, OfficeCLIError) as error:
            return self._failed(name, error)
        return DiagnosticCheck(
            name=name,
            status=DiagnosticStatus.PASS,
            message=f"{format.value.title()} smoke render passed",
        )

    @staticmethod
    def _failed(name: str, error: Exception) -> DiagnosticCheck:
        return DiagnosticCheck(
            name=name,
            status=DiagnosticStatus.FAIL,
            message=redact_officecli_message(str(error)),
        )

    @staticmethod
    def _skipped(names: tuple[str, ...], reason: str) -> list[DiagnosticCheck]:
        return [
            DiagnosticCheck(
                name=name,
                status=DiagnosticStatus.SKIP,
                message=f"Skipped because the {reason}",
            )
            for name in names
        ]
