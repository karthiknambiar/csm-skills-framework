"""Base abstractions used to author CSAF skills."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel

from csaf.memory import MemoryStore
from csaf.schemas import MemoryRecord
from csaf.skills.types import SkillMetadata, SkillResultDraft

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class SkillContext:
    """Dependencies and retrieved customer context supplied by the runner."""

    execution_id: UUID
    customer_id: str
    memory: MemoryStore
    supporting_memory: tuple[MemoryRecord, ...]


class Skill(ABC, Generic[InputT, OutputT]):
    """Base class for a typed, focused, and composable capability."""

    metadata: SkillMetadata
    input_model: type[InputT]
    output_model: type[OutputT]

    @abstractmethod
    def execute(self, skill_input: InputT, context: SkillContext) -> SkillResultDraft[OutputT]:
        """Perform the skill task without directly committing memory updates."""
