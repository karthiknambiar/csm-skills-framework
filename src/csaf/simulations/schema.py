"""Strict, versioned contracts for deterministic simulation scenarios."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from csaf.schemas import MemoryRecordCreate

NonEmptyString = Annotated[str, Field(min_length=1)]


class SeedMemoryStep(BaseModel):
    """Append memory records directly to the simulated world."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NonEmptyString | None = None
    type: Literal["seed_memory"]
    records: tuple[MemoryRecordCreate, ...]


class RunSkillStep(BaseModel):
    """Run one registered skill with JSON-compatible input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NonEmptyString | None = None
    type: Literal["run_skill"]
    skill: NonEmptyString
    input: dict[str, JsonValue]
    expect_error: str | None = None


class AdvanceTimeStep(BaseModel):
    """Advance the deterministic clock by a bounded duration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NonEmptyString | None = None
    type: Literal["advance_time"]
    seconds: int = Field(gt=0, le=31_536_000)


FaultName = Literal[
    "office_missing",
    "office_render_failure",
    "artifact_commit_failure",
    "connector_timeout",
    "connector_rate_limit",
    "corrupt_template",
]


class SetFaultStep(BaseModel):
    """Enable a bounded deterministic fault injection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NonEmptyString | None = None
    type: Literal["set_fault"]
    fault: FaultName
    remaining_calls: int = Field(default=1, ge=1, le=100)


class ClearFaultsStep(BaseModel):
    """Disable all active deterministic faults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NonEmptyString | None = None
    type: Literal["clear_faults"]


class IngestFixtureStep(BaseModel):
    """Ingest a named connector fixture for one customer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NonEmptyString | None = None
    type: Literal["ingest_fixture"]
    customer_id: NonEmptyString
    fixture: NonEmptyString


SimulationStep = Annotated[
    SeedMemoryStep
    | RunSkillStep
    | AdvanceTimeStep
    | SetFaultStep
    | ClearFaultsStep
    | IngestFixtureStep,
    Field(discriminator="type"),
]


class _ExpectationBase(BaseModel):
    """Shared optional targeting for deterministic expectations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: NonEmptyString | None = None


class OutputEqualsExpectation(_ExpectationBase):
    """Require a structured output path to equal a JSON value."""

    type: Literal["output_equals"]
    path: NonEmptyString
    value: JsonValue


class OutputPresentExpectation(_ExpectationBase):
    """Require a structured output path to be present."""

    type: Literal["output_present"]
    path: NonEmptyString


class ForbiddenTermExpectation(_ExpectationBase):
    """Reject a term anywhere in the graded output."""

    type: Literal["forbidden_term"]
    term: NonEmptyString


class MemoryCountExpectation(_ExpectationBase):
    """Require an exact final memory-record count for a customer."""

    type: Literal["memory_count"]
    customer_id: NonEmptyString
    count: int = Field(ge=0)


class MemoryRevisionExpectation(_ExpectationBase):
    """Require the latest revision of one logical memory record."""

    type: Literal["memory_revision"]
    customer_id: NonEmptyString
    logical_key: NonEmptyString
    revision: int = Field(ge=1)


class ArtifactTypesExpectation(_ExpectationBase):
    """Require an ordered, non-empty sequence of artifact types."""

    type: Literal["artifact_types"]
    values: tuple[NonEmptyString, ...] = Field(min_length=1)


class CitationMinimumExpectation(_ExpectationBase):
    """Require at least a given number of citations."""

    type: Literal["citation_minimum"]
    count: int = Field(ge=1)


class NoCrossCustomerDataExpectation(_ExpectationBase):
    """Require all outputs and effects to stay within customer boundaries."""

    type: Literal["no_cross_customer_data"]


class NoPartialEffectsExpectation(_ExpectationBase):
    """Require a failed step to leave no partially committed effects."""

    type: Literal["no_partial_effects"]
    step_id: NonEmptyString


SimulationExpectation = Annotated[
    OutputEqualsExpectation
    | OutputPresentExpectation
    | ForbiddenTermExpectation
    | MemoryCountExpectation
    | MemoryRevisionExpectation
    | ArtifactTypesExpectation
    | CitationMinimumExpectation
    | NoCrossCustomerDataExpectation
    | NoPartialEffectsExpectation,
    Field(discriminator="type"),
]


class SimulationScenario(BaseModel):
    """One deterministic, versioned customer journey and its expectations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    id: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
    title: NonEmptyString
    seed: int
    customers: tuple[NonEmptyString, ...] = Field(min_length=1)
    steps: tuple[SimulationStep, ...] = Field(min_length=1)
    expectations: tuple[SimulationExpectation, ...] = Field(min_length=1)

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_schema_version_one(cls, value: object) -> object:
        """Reject values merely coercible or equal to the supported version."""

        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_unique_identifiers(self) -> "SimulationScenario":
        """Reject ambiguous customer and explicit step identifiers."""

        if len(set(self.customers)) != len(self.customers):
            raise ValueError("customers must be unique")

        step_ids = [step.id for step in self.steps if step.id is not None]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step ids must be unique")
        return self
