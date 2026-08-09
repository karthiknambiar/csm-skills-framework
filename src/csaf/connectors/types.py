"""Validated contracts for connector discovery, pagination, and normalization."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, field_validator

from csaf.schemas import MemoryKind


class AuthenticationKind(StrEnum):
    """Authentication strategies connectors can advertise."""

    NONE = "none"
    API_KEY = "api_key"
    BASIC = "basic"
    OAUTH2 = "oauth2"


class ConnectorCredentials(BaseModel):
    """Transport-neutral credentials whose values remain redacted in logs."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, SecretStr] = Field(default_factory=dict)


class ConnectorMetadata(BaseModel):
    """Discoverable connector identity and capabilities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=100)
    description: str = Field(min_length=1, max_length=1_000)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    authentication: AuthenticationKind = AuthenticationKind.NONE
    source_types: tuple[str, ...]
    supports_incremental_sync: bool = True


class ConnectorRecord(BaseModel):
    """A raw record emitted by a connector before domain normalization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    external_id: str = Field(min_length=1, max_length=1_000)
    source_type: str = Field(min_length=1, max_length=100)
    source_uri: str | None = Field(default=None, max_length=2_000)
    payload: dict[str, JsonValue]


class NormalizedRecord(BaseModel):
    """Vendor-neutral customer record ready to append to Customer Memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MemoryKind
    content: str = Field(min_length=1)
    logical_key: str | None = Field(default=None, max_length=500)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class ConnectorPage(BaseModel):
    """One deterministic page of raw records and its continuation cursor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[ConnectorRecord, ...]
    next_cursor: str | None = None
    checkpoint_cursor: str | None = None


class ConnectorCheckpoint(BaseModel):
    """Opaque resumable state owned by one connector instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector_name: str
    cursor: str | None = None
    state: dict[str, JsonValue] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IngestionResult(BaseModel):
    """Summary returned after an ingestion run commits normalized records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector_name: str
    customer_id: str
    records_seen: int
    records_written: int
    checkpoint: ConnectorCheckpoint
    memory_record_ids: tuple[str, ...]


class LocalConnectorConfig(BaseModel):
    """Shared configuration for local JSON, CSV, and Markdown connectors."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: Path
    default_kind: MemoryKind = MemoryKind.TIMELINE
