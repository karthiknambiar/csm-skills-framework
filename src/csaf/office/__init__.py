"""Artifact generation boundary and OfficeCLI adapter."""

from csaf.office.base import OfficeArtifactRenderer
from csaf.office.diagnostics import (
    DiagnosticCheck,
    DiagnosticStatus,
    OfficeCLIDoctor,
    OfficeDiagnosticReport,
)
from csaf.office.officecli import OfficeCLIArtifactRenderer, OfficeCLIConfig, OfficeCLIError
from csaf.office.types import OfficeFormat, OfficeOperation, OfficeRenderRequest, OfficeSection

__all__ = [
    "DiagnosticCheck",
    "DiagnosticStatus",
    "OfficeArtifactRenderer",
    "OfficeCLIArtifactRenderer",
    "OfficeCLIConfig",
    "OfficeCLIError",
    "OfficeCLIDoctor",
    "OfficeDiagnosticReport",
    "OfficeFormat",
    "OfficeOperation",
    "OfficeRenderRequest",
    "OfficeSection",
]
