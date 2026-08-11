"""Public contracts for CSAF native setup."""

from csaf.setup.assets import (
    AssetLimits,
    SetupError,
    download_verified,
    extract_verified_archive,
    read_json,
    write_json_atomic,
)
from csaf.setup.paths import (
    codex_skill_root,
    current_platform,
    default_data_root,
    detect_assistants,
)
from csaf.setup.types import (
    AssistantKind,
    InstallState,
    OfficeCLIDependency,
    ReleaseAsset,
    ReleaseManifest,
    SupportedPlatform,
    Version,
)

__all__ = [
    "AssetLimits",
    "AssistantKind",
    "codex_skill_root",
    "current_platform",
    "default_data_root",
    "detect_assistants",
    "download_verified",
    "extract_verified_archive",
    "InstallState",
    "OfficeCLIDependency",
    "ReleaseAsset",
    "ReleaseManifest",
    "read_json",
    "SetupError",
    "SupportedPlatform",
    "Version",
    "write_json_atomic",
]
