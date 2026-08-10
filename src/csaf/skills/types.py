"""Validated contracts shared by skill authors and callers."""

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from csaf.schemas import MemoryKind, MemoryRecord, MemoryRecordCreate

OutputT = TypeVar("OutputT", bound=BaseModel)


class ArtifactType(StrEnum):
    """Artifact formats a skill may declare or produce."""

    MARKDOWN = "markdown"
    WORD = "word"
    POWERPOINT = "powerpoint"
    EXCEL = "excel"
    PDF = "pdf"
    HTML = "html"


class Artifact(BaseModel):
    """A generated artifact returned independently from structured output."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    type: ArtifactType
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=200)
    content: bytes


ArtifactHandler = Callable[[tuple[Artifact, ...]], None]


class SkillMetadata(BaseModel):
    """Machine-readable capability and effect declaration for a skill."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=100)
    description: str = Field(min_length=1, max_length=1_000)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    required_inputs: tuple[str, ...] = ()
    optional_inputs: tuple[str, ...] = ()
    memory_reads: tuple[MemoryKind, ...] = ()
    memory_writes: tuple[MemoryKind, ...] = ()
    latest_memory_only: bool = True
    artifacts: tuple[ArtifactType, ...] = ()
    evaluation_tests: tuple[str, ...] = ()

    @model_validator(mode="after")
    def ensure_input_declarations_do_not_overlap(self) -> "SkillMetadata":
        """Reject ambiguous declarations and duplicate field names."""

        required = set(self.required_inputs)
        optional = set(self.optional_inputs)
        if len(required) != len(self.required_inputs):
            raise ValueError("required_inputs must not contain duplicates")
        if len(optional) != len(self.optional_inputs):
            raise ValueError("optional_inputs must not contain duplicates")
        if overlap := required & optional:
            raise ValueError(f"inputs cannot be both required and optional: {sorted(overlap)}")
        return self


class SkillResultDraft(BaseModel, Generic[OutputT]):
    """A skill's proposed result before memory effects are committed."""

    model_config = ConfigDict(extra="forbid")

    output: OutputT
    memory_updates: tuple[MemoryRecordCreate, ...] = ()
    artifacts: tuple[Artifact, ...] = ()


class SkillRunResult(BaseModel, Generic[OutputT]):
    """Validated result returned after the runner commits memory updates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: UUID
    skill_name: str
    skill_version: str
    started_at: datetime
    completed_at: datetime
    output: OutputT
    memory_updates: tuple[MemoryRecord, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
