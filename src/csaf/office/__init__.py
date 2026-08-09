"""Artifact generation boundary and OfficeCLI adapter."""

from csaf.office.base import OfficeArtifactRenderer
from csaf.office.officecli import OfficeCLIArtifactRenderer, OfficeCLIConfig, OfficeCLIError
from csaf.office.types import OfficeFormat, OfficeOperation, OfficeRenderRequest, OfficeSection

__all__ = [
    "OfficeArtifactRenderer",
    "OfficeCLIArtifactRenderer",
    "OfficeCLIConfig",
    "OfficeCLIError",
    "OfficeFormat",
    "OfficeOperation",
    "OfficeRenderRequest",
    "OfficeSection",
]
