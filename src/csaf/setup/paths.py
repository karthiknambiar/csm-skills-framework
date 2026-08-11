"""Platform and assistant discovery with injectable host inputs."""

from __future__ import annotations

import os
import platform
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from csaf.setup.types import AssistantKind, SupportedPlatform


def current_platform(system: str | None = None, machine: str | None = None) -> SupportedPlatform:
    """Return the release platform matching the current or supplied host."""
    system_name = (system if system is not None else platform.system()).casefold()
    machine_name = (machine if machine is not None else platform.machine()).casefold()
    operating_systems = {
        "windows": "windows",
        "darwin": "macos",
        "macos": "macos",
        "linux": "linux",
    }
    architectures = {
        "amd64": "x64",
        "x64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    try:
        normalized_system = operating_systems[system_name]
    except KeyError as error:
        raise ValueError(f"unsupported operating system: {system_name or '<empty>'}") from error
    try:
        normalized_machine = architectures[machine_name]
    except KeyError as error:
        raise ValueError(f"unsupported architecture: {machine_name or '<empty>'}") from error
    return SupportedPlatform(f"{normalized_system}-{normalized_machine}")


def default_data_root(
    *, system: str | None = None, home: Path | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """Return the native data root; Windows falls back to ``~/AppData/Local``."""
    system_name = (system if system is not None else platform.system()).casefold()
    user_home = Path.home() if home is None else Path(home)
    environment = os.environ if environ is None else environ
    configured_root = environment.get("CSAF_DATA_ROOT")
    if configured_root:
        root = Path(configured_root)
        if not root.is_absolute():
            raise ValueError("CSAF_DATA_ROOT must be absolute")
        if root == Path(root.anchor):
            raise ValueError("CSAF_DATA_ROOT must not be a filesystem root")
        return root
    if system_name == "windows":
        base = (
            Path(environment["LOCALAPPDATA"])
            if environment.get("LOCALAPPDATA")
            else user_home / "AppData" / "Local"
        )
        return base / "CSAF"
    if system_name in {"darwin", "macos"}:
        return user_home / "Library" / "Application Support" / "CSAF"
    if system_name == "linux":
        configured = (
            Path(environment["XDG_DATA_HOME"]) if environment.get("XDG_DATA_HOME") else None
        )
        base = (
            configured
            if configured is not None and configured.is_absolute()
            else user_home / ".local" / "share"
        )
        return base / "csaf"
    raise ValueError(f"unsupported operating system: {system_name or '<empty>'}")


def codex_skill_root(*, home: Path | None = None, environ: Mapping[str, str] | None = None) -> Path:
    """Return the configured or conventional Codex skill directory."""
    user_home = Path.home() if home is None else Path(home)
    environment = os.environ if environ is None else environ
    codex_home = (
        Path(environment["CODEX_HOME"]) if environment.get("CODEX_HOME") else user_home / ".codex"
    )
    return codex_home / "skills"


def detect_assistants(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[AssistantKind, ...]:
    """Detect assistants in deterministic enum order from injectable inputs."""
    user_home = Path.home() if home is None else Path(home)
    environment = os.environ if environ is None else environ
    available = {
        AssistantKind.CODEX: bool(environment.get("CODEX_HOME"))
        or codex_skill_root(home=user_home, environ=environment).is_dir()
        or which("codex") is not None,
        AssistantKind.CLAUDE: (user_home / ".claude").is_dir() or which("claude") is not None,
    }
    return tuple(assistant for assistant in AssistantKind if available[assistant])
