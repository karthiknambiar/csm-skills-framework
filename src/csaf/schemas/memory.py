"""Validated contracts for append-only Customer Memory."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)


class MemoryKind(StrEnum):
    """Initial normalized categories stored in Customer Memory."""

    PROFILE = "profile"
    STAKEHOLDER = "stakeholder"
    MEETING = "meeting"
    SUPPORT = "support"
    TIMELINE = "timeline"
    PRODUCT_USAGE = "product_usage"
    ROADMAP = "roadmap"
    COMMITMENT = "commitment"
    RISK = "risk"
    FEATURE_REQUEST = "feature_request"
    QBR = "qbr"
    SUCCESS_PLAN = "success_plan"
    RENEWAL = "renewal"
    HEALTH = "health"
    ARTIFACT = "artifact"


class SourceReference(BaseModel):
    """Provenance pointing from a memory claim to its source material."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=500)
    uri: str | None = Field(default=None, max_length=2_000)
    excerpt: str | None = Field(default=None, max_length=10_000)
    occurred_at: datetime | None = None

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        """Ensure source timestamps identify an unambiguous instant."""

        if value is not None and value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value


class MemoryRecordCreate(BaseModel):
    """Input used to append a new immutable memory revision."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=200)
    kind: MemoryKind
    content: str = Field(min_length=1)
    logical_key: str | None = Field(default=None, max_length=500)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    sources: tuple[SourceReference, ...] = ()
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    occurred_at: datetime | None = None

    @field_validator("customer_id", "content", "logical_key", mode="before")
    @classmethod
    def reject_blank_strings(cls, value: object) -> object:
        """Reject whitespace-only identifiers, keys, and content."""

        if isinstance(value, str) and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        """Ensure event timestamps identify an unambiguous instant."""

        if value is not None and value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value


class MemoryRecord(MemoryRecordCreate):
    """A persisted memory record; revisions are immutable and append-only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    revision: int = Field(ge=1)
    created_at: datetime = Field(default_factory=utc_now)


class MemoryQuery(BaseModel):
    """Structured filters for deterministic memory retrieval."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=200)
    kinds: tuple[MemoryKind, ...] = ()
    text: str | None = Field(default=None, min_length=1)
    since: datetime | None = None
    until: datetime | None = None
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    latest_only: bool = False
    limit: int = Field(default=100, ge=1, le=1_000)

    @field_validator("customer_id", "text", mode="before")
    @classmethod
    def reject_blank_strings(cls, value: object) -> object:
        """Reject whitespace-only customer identifiers and search text."""

        if isinstance(value, str) and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("since", "until")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        """Ensure time filters identify unambiguous instants."""

        if value is not None and value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_time_window(self) -> "MemoryQuery":
        """Reject inverted retrieval windows."""

        if self.since and self.until and self.since > self.until:
            raise ValueError("since must be earlier than or equal to until")
        return self
