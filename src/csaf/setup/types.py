"""Strict value contracts for native CSAF setup and release metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import total_ordering
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self, TypeAlias, TypeVar

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    SerializerFunctionWrapHandler,
    StrictBool,
    StrictInt,
    WrapSerializer,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema

_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_VERSION_ERROR = "version must contain exactly three non-negative integer components"


def _require_schema_version(value: Any) -> Any:
    if type(value) is not int:
        raise ValueError("schema version must be the integer 1")
    return value


SchemaVersion = Annotated[Literal[1], BeforeValidator(_require_schema_version)]


@total_ordering
@dataclass(frozen=True)
class Version:
    """A three-component version with numeric ordering."""

    value: str = field(compare=False)
    _parts: tuple[int, int, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _VERSION_PATTERN.fullmatch(self.value):
            raise ValueError(_VERSION_ERROR)
        object.__setattr__(self, "_parts", tuple(map(int, self.value.split("."))))

    def __str__(self) -> str:
        return self.value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._parts < other._parts

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        del source_type, handler
        string_schema = core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
        )
        return core_schema.json_or_python_schema(
            json_schema=string_schema,
            python_schema=core_schema.union_schema(
                [core_schema.is_instance_schema(cls), string_schema]
            ),
            serialization=core_schema.to_string_ser_schema(),
        )


class SupportedPlatform(StrEnum):
    """Platforms for which native release assets are published."""

    WINDOWS_X64 = "windows-x64"
    WINDOWS_ARM64 = "windows-arm64"
    MACOS_X64 = "macos-x64"
    MACOS_ARM64 = "macos-arm64"
    LINUX_X64 = "linux-x64"
    LINUX_ARM64 = "linux-arm64"


class AssistantKind(StrEnum):
    """Native assistants supported by CSAF adapters."""

    CODEX = "codex"
    CLAUDE = "claude"
    GEMINI = "gemini"


MappingKey = TypeVar("MappingKey")
MappingValue = TypeVar("MappingValue")


def _freeze_mapping(
    value: Mapping[MappingKey, MappingValue],
) -> Mapping[MappingKey, MappingValue]:
    return MappingProxyType(dict(value))


def _serialize_mapping(
    value: Mapping[MappingKey, MappingValue],
    handler: SerializerFunctionWrapHandler,
) -> Any:
    return handler(dict(value))


FrozenMapping: TypeAlias = Annotated[
    Mapping[MappingKey, MappingValue],
    AfterValidator(_freeze_mapping),
    WrapSerializer(_serialize_mapping),
]


class ReleaseAsset(BaseModel):
    """A size-bounded, checksummed HTTPS release asset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: AnyHttpUrl
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: StrictInt = Field(gt=0)

    @field_validator("url")
    @classmethod
    def require_https(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https":
            raise ValueError("release asset URL must use HTTPS")
        return value


def _require_all_platforms(
    assets: Mapping[SupportedPlatform, ReleaseAsset],
) -> Mapping[SupportedPlatform, ReleaseAsset]:
    if set(assets) != set(SupportedPlatform):
        raise ValueError("asset mapping must contain exactly every supported platform")
    return assets


class OfficeCLIDependency(BaseModel):
    """Pinned OfficeCLI release and its minimum compatible version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Version
    minimum_version: Version
    assets: FrozenMapping[SupportedPlatform, ReleaseAsset]

    _all_platforms = field_validator("assets")(_require_all_platforms)

    @model_validator(mode="after")
    def require_compatible_pin(self) -> Self:
        if self.version < self.minimum_version:
            raise ValueError("OfficeCLI version must satisfy its minimum version")
        return self


class ReleaseManifest(BaseModel):
    """Authoritative native release metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: SchemaVersion
    version: Version
    runtime: FrozenMapping[SupportedPlatform, ReleaseAsset]
    codex_skill: ReleaseAsset
    claude_plugin: ReleaseAsset
    officecli: OfficeCLIDependency

    _all_runtime_platforms = field_validator("runtime")(_require_all_platforms)


Checksum = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class InstallState(BaseModel):
    """Non-customer installation metadata persisted by native setup."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    schema_version: SchemaVersion = 1
    active_version: Version | None = None
    installed_versions: tuple[Version, ...] = ()
    runtime_paths: FrozenMapping[Version, Path] = Field(default_factory=dict)
    verified_checksums: FrozenMapping[str, Checksum] = Field(default_factory=dict)
    adapter_targets: FrozenMapping[AssistantKind, Path] = Field(default_factory=dict)
    officecli_version: Version | None = None
    officecli_path: Path | None = None
    officecli_sha256: Checksum | None = None
    officecli_installed_by_csaf: StrictBool = False
    installed_at: datetime | None = None
    updated_at: datetime | None = None
