"""In-process registry for discovering skills by stable name."""

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

from csaf.skills.base import Skill
from csaf.skills.errors import DuplicateSkillError, SkillContractError, SkillNotFoundError


class SkillRegistry:
    """Register and discover skill instances without global mutable state."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill[Any, Any]] = {}

    def register(self, skill: Skill[Any, Any]) -> None:
        """Register a skill, rejecting accidental name replacement."""

        name = skill.metadata.name
        if name in self._skills:
            raise DuplicateSkillError(f"skill is already registered: {name}")
        self._validate_contract(skill)
        self._skills[name] = skill

    def get(self, name: str) -> Skill[Any, Any]:
        """Return a registered skill or raise a domain-specific error."""

        try:
            return self._skills[name]
        except KeyError as error:
            raise SkillNotFoundError(f"skill is not registered: {name}") from error

    def names(self) -> tuple[str, ...]:
        """Return deterministic registered names for discovery surfaces."""

        return tuple(sorted(self._skills))

    def __iter__(self) -> Iterator[Skill[Any, Any]]:
        for name in self.names():
            yield self._skills[name]

    def __len__(self) -> int:
        return len(self._skills)

    @staticmethod
    def _validate_contract(skill: Skill[Any, Any]) -> None:
        if not issubclass(skill.input_model, BaseModel):
            raise SkillContractError("input_model must be a Pydantic BaseModel")
        if not issubclass(skill.output_model, BaseModel):
            raise SkillContractError("output_model must be a Pydantic BaseModel")
        fields = skill.input_model.model_fields
        required = {name for name, field in fields.items() if field.is_required()}
        optional = set(fields) - required
        if set(skill.metadata.required_inputs) != required:
            raise SkillContractError("required_inputs must match required input model fields")
        if set(skill.metadata.optional_inputs) != optional:
            raise SkillContractError("optional_inputs must match optional input model fields")
        if "customer_id" not in required:
            raise SkillContractError("customer_id must be a required input model field")
