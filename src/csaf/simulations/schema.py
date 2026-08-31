"""Strict, versioned contracts for deterministic simulation scenarios."""

from math import isfinite
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    SerializationInfo,
    StrictInt,
    field_serializer,
    field_validator,
    model_validator,
)

from csaf.schemas import MemoryRecordCreate

NonEmptyString = Annotated[str, Field(min_length=1, pattern=r"\S")]


class _FrozenJsonDict(dict[str, object]):
    """A JSON object that rejects mutation through normal mapping operations."""

    def _reject_mutation(self, *_: object, **__: object) -> None:
        raise TypeError("simulation JSON values are immutable")

    def __copy__(self) -> "_FrozenJsonDict":
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> "_FrozenJsonDict":
        memo[id(self)] = self
        return self

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __ior__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation


def _freeze_json(value: object) -> object:
    """Recursively freeze validated JSON containers."""

    if isinstance(value, float) and not isfinite(value):
        raise ValueError("JSON floats must be finite")
    if isinstance(value, dict):
        return _FrozenJsonDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _dump_json(value: object) -> object:
    """Convert immutable JSON containers back to ordinary JSON containers."""

    if isinstance(value, dict):
        return {key: _dump_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_dump_json(item) for item in value]
    return value


_FrozenJsonValue = Annotated[
    JsonValue,
    AfterValidator(_freeze_json),
    PlainSerializer(_dump_json, return_type=JsonValue),
]
_FrozenJsonObject = Annotated[
    dict[str, JsonValue],
    AfterValidator(_freeze_json),
    PlainSerializer(_dump_json, return_type=dict[str, JsonValue]),
]


class _SimulationMemoryRecordCreate(MemoryRecordCreate):
    """Immutable simulation fixture compatible with domain memory inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    metadata: _FrozenJsonObject = Field(default_factory=dict)


class SeedMemoryStep(BaseModel):
    """Append memory records directly to the simulated world."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NonEmptyString | None = None
    type: Literal["seed_memory"]
    records: tuple[MemoryRecordCreate, ...]

    @field_validator("records", mode="after")
    @classmethod
    def freeze_domain_records(
        cls, value: tuple[MemoryRecordCreate, ...]
    ) -> tuple[MemoryRecordCreate, ...]:
        """Convert mutable domain record instances into frozen simulation copies."""

        fields = set(MemoryRecordCreate.model_fields)
        return tuple(
            _SimulationMemoryRecordCreate.model_validate(record.model_dump(include=fields))
            for record in value
        )

    @field_serializer("records")
    def serialize_records(
        self, records: tuple[MemoryRecordCreate, ...], info: SerializationInfo
    ) -> tuple[dict[str, object], ...]:
        """Serialize private frozen records through ordinary JSON payloads."""

        return tuple(record.model_dump(mode=info.mode) for record in records)


class RunSkillStep(BaseModel):
    """Run one registered skill with JSON-compatible input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NonEmptyString | None = None
    type: Literal["run_skill"]
    skill: NonEmptyString
    input: _FrozenJsonObject
    expect_error: NonEmptyString | None = None


class AdvanceTimeStep(BaseModel):
    """Advance the deterministic clock by a bounded duration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NonEmptyString | None = None
    type: Literal["advance_time"]
    seconds: StrictInt = Field(gt=0, le=31_536_000)


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
    remaining_calls: StrictInt = Field(default=1, ge=1, le=100)


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
    value: _FrozenJsonValue


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
    count: StrictInt = Field(ge=0)


class MemoryRevisionExpectation(_ExpectationBase):
    """Require the latest revision of one logical memory record."""

    type: Literal["memory_revision"]
    customer_id: NonEmptyString
    logical_key: NonEmptyString
    revision: StrictInt = Field(ge=1)


class ArtifactTypesExpectation(_ExpectationBase):
    """Require an ordered, non-empty sequence of artifact types."""

    type: Literal["artifact_types"]
    values: tuple[NonEmptyString, ...] = Field(min_length=1)


class CitationMinimumExpectation(_ExpectationBase):
    """Require at least a given number of citations."""

    type: Literal["citation_minimum"]
    count: StrictInt = Field(ge=1)


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
    seed: StrictInt
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
    def validate_references_and_unique_identifiers(self) -> "SimulationScenario":
        """Reject ambiguous identifiers and references outside the scenario."""

        if len(set(self.customers)) != len(self.customers):
            raise ValueError("customers must be unique")

        step_ids = [step.id for step in self.steps if step.id is not None]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step ids must be unique")

        explicit_step_ids = set(step_ids)
        for expectation in self.expectations:
            if expectation.step_id is not None and expectation.step_id not in explicit_step_ids:
                raise ValueError("expectation step_id must reference an explicit step")

        customer_ids = set(self.customers)
        for step in self.steps:
            if isinstance(step, SeedMemoryStep):
                referenced_customer_ids = (record.customer_id for record in step.records)
            elif isinstance(step, IngestFixtureStep):
                referenced_customer_ids = (step.customer_id,)
            elif isinstance(step, RunSkillStep):
                customer_id = step.input.get("customer_id")
                referenced_customer_ids = (customer_id,) if isinstance(customer_id, str) else ()
            else:
                referenced_customer_ids = ()
            if any(customer_id not in customer_ids for customer_id in referenced_customer_ids):
                raise ValueError("customer_id must belong to scenario customers")

        for expectation in self.expectations:
            if isinstance(expectation, MemoryCountExpectation | MemoryRevisionExpectation):
                if expectation.customer_id not in customer_ids:
                    raise ValueError("customer_id must belong to scenario customers")
        return self
