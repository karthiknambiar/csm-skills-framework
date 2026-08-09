"""Contract-enforcing lifecycle runner for registered skills."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from csaf.memory import MemoryStore
from csaf.schemas import MemoryQuery
from csaf.skills.base import SkillContext
from csaf.skills.errors import SkillContractError
from csaf.skills.registry import SkillRegistry
from csaf.skills.types import SkillResultDraft, SkillRunResult


class SkillRunner:
    """Validate, retrieve context, execute, commit effects, and return a result."""

    def __init__(self, registry: SkillRegistry, memory: MemoryStore) -> None:
        self._registry = registry
        self._memory = memory

    def run(self, name: str, raw_input: BaseModel | Mapping[str, Any]) -> SkillRunResult[Any]:
        """Run a registered skill through the standard lifecycle."""

        skill = self._registry.get(name)
        started_at = datetime.now(UTC)
        execution_id = uuid4()
        try:
            skill_input = skill.input_model.model_validate(raw_input)
        except ValidationError as error:
            raise SkillContractError(f"invalid input for skill {name}: {error}") from error

        customer_id = getattr(skill_input, "customer_id", None)
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise SkillContractError(f"skill input must expose a non-blank customer_id: {name}")

        supporting_memory = (
            tuple(
                self._memory.search(
                    MemoryQuery(
                        customer_id=customer_id,
                        kinds=skill.metadata.memory_reads,
                        latest_only=skill.metadata.latest_memory_only,
                    )
                )
            )
            if skill.metadata.memory_reads
            else ()
        )
        context = SkillContext(
            execution_id=execution_id,
            customer_id=customer_id,
            memory=self._memory,
            supporting_memory=supporting_memory,
        )
        draft = skill.execute(skill_input, context)
        if not isinstance(draft, SkillResultDraft):
            raise SkillContractError(f"skill must return SkillResultDraft: {name}")
        try:
            output = skill.output_model.model_validate(draft.output)
        except ValidationError as error:
            raise SkillContractError(f"invalid output from skill {name}: {error}") from error

        self._validate_effects(name, customer_id, draft)
        updates = tuple(self._memory.append(update) for update in draft.memory_updates)
        return SkillRunResult[Any](
            execution_id=execution_id,
            skill_name=skill.metadata.name,
            skill_version=skill.metadata.version,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            output=output,
            memory_updates=updates,
            artifacts=draft.artifacts,
        )

    def _validate_effects(
        self,
        name: str,
        customer_id: str,
        draft: SkillResultDraft[Any],
    ) -> None:
        skill = self._registry.get(name)
        allowed_writes = set(skill.metadata.memory_writes)
        allowed_artifacts = set(skill.metadata.artifacts)
        for update in draft.memory_updates:
            if update.customer_id != customer_id:
                raise SkillContractError("a skill cannot write memory for another customer")
            if update.kind not in allowed_writes:
                raise SkillContractError(
                    f"undeclared memory write from skill {name}: {update.kind}"
                )
        for artifact in draft.artifacts:
            if artifact.type not in allowed_artifacts:
                raise SkillContractError(
                    f"undeclared artifact type from skill {name}: {artifact.type}"
                )
