"""Validated golden-case, score, and regression-report contracts."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, computed_field, model_validator

from csaf.schemas import MemoryKind, MemoryRecordCreate
from csaf.skills import ArtifactType


class EvaluationCategory(StrEnum):
    """Quality dimensions measured without requiring an LLM grader."""

    ACCURACY = "accuracy"
    COMPLETENESS = "completeness"
    HALLUCINATION = "hallucination"
    CITATION_QUALITY = "citation_quality"
    CONSISTENCY = "consistency"
    MEMORY_UPDATES = "memory_updates"


class EvaluationCase(BaseModel):
    """One reproducible skill input, memory fixture, and expected behavior."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=150)
    skill_name: str
    input: dict[str, JsonValue]
    memory: tuple[MemoryRecordCreate, ...] = ()
    expected_values: dict[str, JsonValue] = Field(default_factory=dict)
    required_output_paths: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    minimum_citations: int = Field(default=0, ge=0)
    expected_memory_writes: dict[MemoryKind, int] = Field(default_factory=dict)
    expected_artifacts: tuple[ArtifactType, ...] = ()
    minimum_scores: dict[EvaluationCategory, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_score_thresholds(self) -> "EvaluationCase":
        for category, score in self.minimum_scores.items():
            if score < 0.0 or score > 1.0:
                raise ValueError(f"minimum score for {category} must be between 0 and 1")
        return self


class EvaluationFinding(BaseModel):
    """One human-readable assertion outcome contributing to a score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: EvaluationCategory
    passed: bool
    message: str


class EvaluationResult(BaseModel):
    """Scores and findings for one golden case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_name: str
    skill_name: str
    passed: bool
    scores: dict[EvaluationCategory, float]
    findings: tuple[EvaluationFinding, ...]
    duration_ms: float = Field(ge=0.0)


class EvaluationReport(BaseModel):
    """CI-serializable aggregate regression report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    cases_total: int = Field(ge=0)
    cases_passed: int = Field(ge=0)
    results: tuple[EvaluationResult, ...]

    @computed_field
    @property
    def pass_rate(self) -> float:
        return self.cases_passed / self.cases_total if self.cases_total else 1.0
