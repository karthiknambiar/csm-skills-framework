"""Format-neutral document request passed to Office artifact adapters."""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OfficeFormat(StrEnum):
    """Office formats supported by the initial adapter."""

    POWERPOINT = "powerpoint"
    WORD = "word"


class OfficeOperation(StrEnum):
    """Whether OfficeCLI creates a file or updates an existing one."""

    CREATE = "create"
    UPDATE = "update"


class OfficeSection(BaseModel):
    """A titled block with bullets and durable memory citations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    bullets: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()


class OfficeRenderRequest(BaseModel):
    """Portable document specification consumed by an Office renderer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: OfficeFormat
    operation: OfficeOperation = OfficeOperation.CREATE
    title: str = Field(min_length=1, max_length=300)
    subtitle: str | None = Field(default=None, max_length=500)
    sections: tuple[OfficeSection, ...]
    template_path: Path | None = None
    existing_path: Path | None = None

    @model_validator(mode="after")
    def validate_operation_paths(self) -> "OfficeRenderRequest":
        if self.operation is OfficeOperation.UPDATE and self.existing_path is None:
            raise ValueError("existing_path is required for update operations")
        if self.operation is OfficeOperation.CREATE and self.existing_path is not None:
            raise ValueError("existing_path is only valid for update operations")
        return self
