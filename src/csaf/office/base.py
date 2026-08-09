"""Replaceable Office artifact rendering contract."""

from typing import Protocol

from csaf.office.types import OfficeRenderRequest


class OfficeArtifactRenderer(Protocol):
    """Render a validated, format-neutral request into Office file bytes."""

    def render(self, request: OfficeRenderRequest) -> bytes:
        """Create or update an Office artifact and return its bytes."""
