"""Consent-first, component-aware orchestration for native CSAF setup."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from csaf.setup.adapters import (
    _MARKETPLACE_SOURCE as _CLAUDE_SOURCE,
)
from csaf.setup.adapters import (
    AdapterInstallResult,
    ClaudeAdapterInstaller,
    CodexAdapterInstaller,
    CommandRunner,
    GeminiAdapterInstaller,
    _subprocess_runner,
)
from csaf.setup.assets import (
    AssetLimits,
    SetupError,
    _acquire_activation_lock,
    _fsync_directory,
    _prepare_private_parent,
    _reject_linked_parents,
    _release_activation_lock,
    download_verified,
    extract_verified_archive,
    read_json,
    write_json_atomic,
)
from csaf.setup.types import (
    AssistantKind,
    InstallState,
    ReleaseAsset,
    ReleaseManifest,
    SupportedPlatform,
    Version,
)

_OFFICE_VERSION = Version("1.0.143")
_OFFICE_MINIMUM = Version("1.0.137")
_MARKER = ".csaf-runtime.sha256"


class SetupStatus(StrEnum):
    READY = "ready"
    CANCELLED = "cancelled"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class InstallPlan:
    manifest: ReleaseManifest
    platform: SupportedPlatform
    targets: tuple[AssistantKind, ...]
    data_root: Path
    runtime_asset: ReleaseAsset
    officecli_asset: ReleaseAsset
    adapter_assets: Mapping[AssistantKind, ReleaseAsset]
    adapter_destinations: Mapping[AssistantKind, Path]
    runtime_path: Path
    officecli_path: Path
    already_healthy: bool = False


@dataclass(frozen=True, slots=True)
class SetupResult:
    status: SetupStatus
    active_version: Version | None
    activated: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Health:
    runtime: bool
    officecli: bool
    adapters: Mapping[AssistantKind, bool]

    @property
    def ready(self) -> bool:
        return self.runtime and self.officecli and all(self.adapters.values())


@dataclass(frozen=True, slots=True)
class _AdapterBackup:
    kind: AssistantKind
    target: Path
    backup: Path | None
    version: Version | None
    checksum: str | None
    fresh_target: bool
    ownership: Mapping[str, bool]
    recovery: Mapping[str, object]


class Downloader(Protocol):
    def __call__(self, asset: ReleaseAsset, destination: Path) -> Path: ...


class RuntimeInstaller(Protocol):
    def __call__(self, bundle: Path, destination: Path, expected_version: Version) -> Path: ...


class ManagedAdapter(Protocol):
    kind: AssistantKind
    destination: Path

    def install(self, asset: Path, version: Version) -> AdapterInstallResult: ...
    def health(self, target: Path, version: Version, sha256: str) -> bool: ...
    def uninstall(self, target: Path) -> None: ...


class DoctorRunner(Protocol):
    def __call__(self, runtime: Path, officecli: Path, environment: dict[str, str]) -> bool: ...


RuntimeProbe = Callable[[Path, Version], bool]
OfficeProbe = Callable[[Path, Version, Version], bool]
JsonWriter = Callable[[Path, object], None]
Clock = Callable[[], datetime]
Checkpoint = Callable[[str], None]
Consent = Callable[[InstallPlan], bool]


def _missing_runtime(bundle: Path, destination: Path, expected_version: Version) -> Path:
    del bundle, destination, expected_version
    raise SetupError("runtime installer is unavailable")


def _runtime_probe(runtime: Path, version: Version) -> bool:
    del version
    return (runtime / ("csaf.exe" if os.name == "nt" else "csaf")).is_file()


def _office_probe(path: Path, version: Version, minimum: Version) -> bool:
    if version != _OFFICE_VERSION or minimum < _OFFICE_MINIMUM:
        return False
    environment = os.environ.copy()
    environment.update(SetupManager._office_environment(path))
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(
                [str(path), "--version"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                timeout=30.0,
                env=environment,
            )
            output.seek(0)
            value = output.read(4097)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and len(value) <= 4096 and b"1.0.143" in value


def _doctor(runtime: Path, officecli: Path, environment: dict[str, str]) -> bool:
    executable = runtime / ("csaf.exe" if os.name == "nt" else "csaf")
    process_environment = os.environ.copy()
    process_environment.update(environment)
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(
                [str(executable), "office", "doctor", "--json"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                timeout=120.0,
                env=process_environment,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError("setup diagnostics failed") from error
    return completed.returncode == 0 and officecli.is_file()


_ADAPTER_LIMITS = AssetLimits(
    max_archive_bytes=64 * 1024 * 1024,
    max_members=4096,
    max_member_bytes=32 * 1024 * 1024,
    max_total_bytes=128 * 1024 * 1024,
)
_CODEX_RECEIPT = ".csaf-adapter.json"
_CLAUDE_RECEIPT = "receipt.json"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def _adapter_tree_digest(root: Path, receipt_name: str) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != receipt_name)
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as source:
            while chunk := source.read(65536):
                digest.update(chunk)
    return digest.hexdigest()


class CodexManagedAdapter:
    """Lifecycle facade for a user-scoped filesystem skill."""

    kind = AssistantKind.CODEX
    _assistant_name = "Codex"
    _slug = "codex"
    _installer_type = CodexAdapterInstaller

    def __init__(self, skill_root: Path, *, checkpoint: Checkpoint | None = None) -> None:
        self._skill_root = Path(skill_root)
        self.destination = self._skill_root / "csaf"
        self._checkpoint = checkpoint or (lambda _phase: None)

    def install(self, asset: Path, version: Version) -> AdapterInstallResult:
        self._checkpoint(f"{self._slug}:entry")
        archive = Path(asset)
        extracted = archive.parent / f"{self._slug}-extracted"
        try:
            extract_verified_archive(archive, extracted, limits=_ADAPTER_LIMITS)
            candidates = [path.parent for path in extracted.rglob("SKILL.md")]
            if len(candidates) != 1:
                raise SetupError(
                    f"{self._assistant_name} adapter asset must contain exactly one skill"
                )
            source = candidates[0]
            receipt = {
                "schema_version": 1,
                "version": str(version),
                "asset_sha256": _file_sha256(archive),
                "content_sha256": _adapter_tree_digest(source, _CODEX_RECEIPT),
            }
            write_json_atomic(source / _CODEX_RECEIPT, receipt)
            result = self._installer_type(source, self._skill_root).install()
            if result.target != self.destination:
                raise SetupError(
                    f"{self._assistant_name} adapter returned an unexpected destination",
                    activated=True,
                )
            return result
        except SetupError:
            raise
        except Exception as error:
            raise SetupError(f"{self._assistant_name} adapter lifecycle failed") from error
        finally:
            if extracted.exists():
                shutil.rmtree(extracted, ignore_errors=True)

    def recovery_snapshot(self, version: Version) -> Mapping[str, object]:
        del version
        _reject_linked_parents(self._skill_root)
        staging = self._skill_root / ".csaf.staging"
        backup = self._skill_root / ".csaf.backup"
        value = {
            "target_existed": self.destination.exists() or self.destination.is_symlink(),
            "task3_staging_existed": staging.exists() or staging.is_symlink(),
            "task3_backup_existed": backup.exists() or backup.is_symlink(),
        }
        if value["task3_staging_existed"] or value["task3_backup_existed"]:
            raise SetupError(f"{self._assistant_name} adapter has unresolved activation artifacts")
        return MappingProxyType(value)

    def reconcile(self, recovery: Mapping[str, object]) -> None:
        expected = {"target_existed", "task3_staging_existed", "task3_backup_existed"}
        if set(recovery) != expected or any(type(recovery[key]) is not bool for key in expected):
            raise SetupError(f"{self._assistant_name} adapter recovery snapshot is invalid")
        if recovery["task3_staging_existed"] or recovery["task3_backup_existed"]:
            raise SetupError(f"{self._assistant_name} adapter recovery snapshot is uncertain")
        _reject_linked_parents(self._skill_root)
        for value in (
            self._skill_root / ".csaf.staging",
            self._skill_root / ".csaf.backup",
            self.destination,
        ):
            if value.exists() or value.is_symlink():
                self._remove_live_path(value)
        _fsync_directory(self._skill_root)

    def _remove_live_path(self, value: Path) -> None:
        _reject_linked_parents(value.parent)
        details = value.lstat()
        attributes = getattr(details, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(details.st_mode) or attributes & reparse_flag:
            try:
                value.unlink()
            except IsADirectoryError:
                os.rmdir(value)
        elif stat.S_ISDIR(details.st_mode):
            _reject_linked_parents(value)
            shutil.rmtree(value)
        elif stat.S_ISREG(details.st_mode):
            value.unlink()

    def health(self, target: Path, version: Version, sha256: str) -> bool:
        if Path(target) != self.destination:
            return False
        try:
            _reject_linked_parents(self.destination)
            receipt = read_json(self.destination / _CODEX_RECEIPT)
            return (
                isinstance(receipt, dict)
                and receipt.get("schema_version") == 1
                and receipt.get("version") == str(version)
                and receipt.get("asset_sha256") == sha256
                and receipt.get("content_sha256")
                == _adapter_tree_digest(self.destination, _CODEX_RECEIPT)
                and (self.destination / "SKILL.md").is_file()
            )
        except Exception:
            return False

    def uninstall(self, target: Path) -> None:
        value = Path(target)
        if value != self.destination:
            raise SetupError(
                f"{self._assistant_name} adapter destination does not match its receipt"
            )
        try:
            _reject_linked_parents(value.parent)
            details = value.lstat()
            attributes = getattr(details, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat.S_ISLNK(details.st_mode) or attributes & reparse_flag:
                try:
                    value.unlink()
                except IsADirectoryError:
                    os.rmdir(value)
            elif stat.S_ISDIR(details.st_mode):
                _reject_linked_parents(value)
                shutil.rmtree(value)
        except OSError as error:
            raise SetupError(f"{self._assistant_name} adapter cleanup failed") from error


class GeminiManagedAdapter(CodexManagedAdapter):
    """Lifecycle facade for Gemini CLI's user-scoped CSAF skill."""

    kind = AssistantKind.GEMINI
    _assistant_name = "Gemini CLI"
    _slug = "gemini"
    _installer_type = GeminiAdapterInstaller


class _ClaudeVersionConflict(SetupError):
    """A safe, actionable Claude ownership conflict for the setup result."""


class _SetupBusy(SetupError):
    """Another setup process owns the lifecycle lock."""


class ClaudeManagedAdapter:
    """Lifecycle facade using the unchanged Task 3 Claude installer and runner."""

    kind = AssistantKind.CLAUDE

    def __init__(
        self,
        destination: Path,
        *,
        runner: CommandRunner = _subprocess_runner,
        timeout: float = 60.0,
        checkpoint: Checkpoint | None = None,
        journal_writer: JsonWriter = write_json_atomic,
    ) -> None:
        self.destination = Path(destination)
        self._runner = runner
        self._timeout = timeout
        self._checkpoint = checkpoint or (lambda _phase: None)
        self._journal_writer = journal_writer

    def _client(self, version: Version) -> ClaudeAdapterInstaller:
        return ClaudeAdapterInstaller(
            version, runner=self._checkpointing_runner, timeout=self._timeout
        )

    def _checkpointing_runner(
        self,
        command: Sequence[str],
        *,
        stdout: object,
        stderr: object,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        result = self._runner(command, stdout=stdout, stderr=stderr, timeout=timeout)
        operation = list(command)[2:]
        phase: str | None = None
        if result.returncode == 0:
            if operation[:2] == ["marketplace", "add"]:
                phase = "claude:marketplace-added"
            elif operation[:2] == ["marketplace", "remove"]:
                phase = "claude:marketplace-removed"
            elif operation[:2] == ["install", "csaf@csaf"]:
                phase = "claude:plugin-installed"
            elif operation[:2] == ["uninstall", "csaf@csaf"]:
                phase = "claude:plugin-removed"
        if phase is not None:
            self._checkpoint(phase)
        return result

    @staticmethod
    def _versioned_receipt(value: object) -> tuple[Version, dict[str, object]] | None:
        if not isinstance(value, dict):
            return None
        version = value.get("version")
        sha256 = value.get("asset_sha256")
        if (
            value.get("schema_version") != 1
            or not isinstance(version, str)
            or not isinstance(sha256, str)
            or len(sha256) != 64
        ):
            return None
        try:
            return Version(version), value
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _state(client: ClaudeAdapterInstaller) -> tuple[bool, bool]:
        marketplaces = client._json_list(
            client._run(
                ["claude", "plugin", "marketplace", "list", "--json"],
                "marketplace list",
            ),
            "marketplace list",
        )
        plugins = client._json_list(
            client._run(["claude", "plugin", "list", "--json"], "plugin list"),
            "plugin list",
        )
        return client._marketplace_present(marketplaces), client._plugin_present(plugins)

    def recovery_snapshot(self, version: Version) -> Mapping[str, object]:
        if self.has_pending_uninstall():
            raise SetupError("Claude adapter has a pending uninstall")
        marketplace, plugin = self._inventory()
        return MappingProxyType(
            {
                "schema_version": 1,
                "installing_version": str(version),
                "marketplace": marketplace,
                "plugin": plugin,
            }
        )

    def _inventory(self) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        client = self._client(Version("0.0.0"))
        marketplaces = client._json_list(
            client._run(
                ["claude", "plugin", "marketplace", "list", "--json"],
                "marketplace list",
            ),
            "marketplace list",
        )
        marketplace: dict[str, object] | None = None
        for entry in marketplaces:
            if not isinstance(entry, Mapping):
                raise SetupError("Claude Code marketplace list returned an unexpected response")
            name = entry.get("name")
            url = entry.get("url")
            ref = entry.get("ref")
            source = entry.get("source")
            collision = (
                name == "csaf"
                or url == _CLAUDE_SOURCE
                or (isinstance(source, str) and source.startswith(_CLAUDE_SOURCE))
            )
            if not collision:
                continue
            normalized_ref: str | None = None
            if (
                name == "csaf"
                and url == _CLAUDE_SOURCE
                and isinstance(ref, str)
                and ref.startswith("v")
                and source in {None, "git"}
            ):
                normalized_ref = ref
            elif (
                name == "csaf"
                and url is None
                and ref is None
                and isinstance(source, str)
                and source.startswith(f"{_CLAUDE_SOURCE}#v")
            ):
                normalized_ref = source.removeprefix(f"{_CLAUDE_SOURCE}#")
            if normalized_ref is None or marketplace is not None:
                raise SetupError("Claude Code CSAF marketplace identity is ambiguous")
            try:
                Version(normalized_ref.removeprefix("v"))
            except ValueError as error:
                raise SetupError("Claude Code CSAF marketplace version is invalid") from error
            marketplace = {
                "name": "csaf",
                "source": _CLAUDE_SOURCE,
                "ref": normalized_ref,
            }
        plugins = client._json_list(
            client._run(["claude", "plugin", "list", "--json"], "plugin list"),
            "plugin list",
        )
        plugin: dict[str, object] | None = None
        for entry in plugins:
            if not isinstance(entry, Mapping):
                raise SetupError("Claude Code plugin list returned an unexpected response")
            name = entry.get("name") or entry.get("id") or entry.get("plugin")
            marketplace_name = entry.get("marketplace") or entry.get("source")
            qualified = name == "csaf@csaf"
            named = name == "csaf" and marketplace_name in {"csaf", "csaf@csaf"}
            if not (qualified or named):
                continue
            scope = entry.get("scope")
            if scope not in {None, "user"} or plugin is not None:
                raise SetupError("Claude Code CSAF plugin identity is ambiguous")
            plugin = {"name": "csaf@csaf", "scope": scope}
        return marketplace, plugin

    @staticmethod
    def _validate_recovery_entry(
        value: object,
        *,
        marketplace: bool,
    ) -> dict[str, object] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise SetupError("Claude adapter recovery snapshot is invalid")
        if marketplace:
            if (
                set(value) != {"name", "source", "ref"}
                or value.get("name") != "csaf"
                or value.get("source") != _CLAUDE_SOURCE
                or not isinstance(value.get("ref"), str)
                or not value["ref"].startswith("v")
            ):
                raise SetupError("Claude adapter recovery snapshot is invalid")
            try:
                Version(value["ref"].removeprefix("v"))
            except ValueError as error:
                raise SetupError("Claude adapter recovery snapshot is invalid") from error
        elif (
            set(value) != {"name", "scope"}
            or value.get("name") != "csaf@csaf"
            or value.get("scope") not in {None, "user"}
        ):
            raise SetupError("Claude adapter recovery snapshot is invalid")
        return dict(value)

    def reconcile(self, recovery: Mapping[str, object]) -> None:
        if set(recovery) != {"schema_version", "installing_version", "marketplace", "plugin"}:
            raise SetupError("Claude adapter recovery snapshot is invalid")
        raw_installing = recovery.get("installing_version")
        if recovery.get("schema_version") != 1 or not isinstance(raw_installing, str):
            raise SetupError("Claude adapter recovery snapshot is invalid")
        try:
            installing = Version(raw_installing)
        except ValueError as error:
            raise SetupError("Claude adapter recovery snapshot is invalid") from error
        prior_marketplace = self._validate_recovery_entry(
            recovery.get("marketplace"), marketplace=True
        )
        prior_plugin = self._validate_recovery_entry(recovery.get("plugin"), marketplace=False)
        current_marketplace, current_plugin = self._inventory()
        allowed_refs = {f"v{installing}"}
        if prior_marketplace is not None:
            allowed_refs.add(str(prior_marketplace["ref"]))
        if current_marketplace is not None and current_marketplace.get("ref") not in allowed_refs:
            raise SetupError("Claude adapter recovery found unexpected external state")
        client = self._client(installing)
        if current_plugin != prior_plugin and current_plugin is not None:
            client._run(
                ["claude", "plugin", "uninstall", "csaf@csaf", "--scope", "user"],
                "plugin recovery",
            )
            current_plugin = None
        if current_marketplace != prior_marketplace:
            if current_marketplace is not None:
                client._run(
                    ["claude", "plugin", "marketplace", "remove", "csaf"],
                    "marketplace recovery",
                )
            if prior_marketplace is not None:
                client._run(
                    [
                        "claude",
                        "plugin",
                        "marketplace",
                        "add",
                        f"{_CLAUDE_SOURCE}#{prior_marketplace['ref']}",
                    ],
                    "marketplace recovery",
                )
        if prior_plugin is not None and current_plugin is None:
            client._run(
                ["claude", "plugin", "install", "csaf@csaf", "--scope", "user"],
                "plugin recovery",
            )
        if self._inventory() != (prior_marketplace, prior_plugin):
            raise SetupError("Claude adapter external recovery could not be verified")
        if self.destination.exists() or self.destination.is_symlink():
            _reject_linked_parents(self.destination)
            shutil.rmtree(self.destination)

    def install(self, asset: Path, version: Version) -> AdapterInstallResult:
        if self.has_pending_uninstall():
            raise SetupError("Claude adapter has a pending uninstall")
        self._checkpoint("claude:entry")
        client = self._client(version)
        try:
            asset_sha256 = _file_sha256(Path(asset))
            prior_marketplace_owned = False
            prior_plugin_owned = False
            if self.destination.exists() or self.destination.is_symlink():
                _reject_linked_parents(self.destination)
            previous_receipt: dict[str, object] | None = None
            previous_version: Version | None = None
            try:
                previous = read_json(self.destination / _CLAUDE_RECEIPT)
                parsed = self._versioned_receipt(previous)
                if parsed is None:
                    raise SetupError("Claude adapter receipt is invalid")
                previous_version, previous_receipt = parsed
                if (
                    previous_version == version
                    and previous_receipt.get("asset_sha256") == asset_sha256
                ):
                    prior_marketplace_owned = previous_receipt.get("marketplace_owned") is True
                    prior_plugin_owned = previous_receipt.get("plugin_owned") is True
            except SetupError:
                if self.destination.exists():
                    raise
            if previous_version is not None and previous_version != version:
                if previous_receipt is None or (
                    previous_receipt.get("marketplace_owned") is not True
                    or previous_receipt.get("plugin_owned") is not True
                ):
                    raise _ClaudeVersionConflict(
                        "Claude adapter version conflict is not CSAF-owned"
                    )
                previous_client = self._client(previous_version)
                try:
                    previous_healthy = self._state(previous_client) == (True, True)
                except SetupError as error:
                    raise _ClaudeVersionConflict(
                        "Claude adapter version conflict is not safely replaceable"
                    ) from error
                if not previous_healthy:
                    raise _ClaudeVersionConflict(
                        "Claude adapter version conflict is not safely replaceable"
                    )
                return self._transition(
                    client=client,
                    version=version,
                    asset_sha256=asset_sha256,
                    previous_client=previous_client,
                    previous_receipt=previous_receipt,
                )
            marketplace_before, plugin_before = self._state(client)
            client.install()
            self.destination.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(self.destination, 0o700)
            receipt = {
                "schema_version": 1,
                "version": str(version),
                "asset_sha256": asset_sha256,
                "marketplace_owned": prior_marketplace_owned or not marketplace_before,
                "plugin_owned": prior_plugin_owned or not plugin_before,
            }
            write_json_atomic(self.destination / _CLAUDE_RECEIPT, receipt)
            return AdapterInstallResult(self.kind, self.destination)
        except SetupError:
            raise
        except Exception as error:
            raise SetupError("Claude adapter lifecycle failed", activated=True) from error

    def _transition(
        self,
        *,
        client: ClaudeAdapterInstaller,
        version: Version,
        asset_sha256: str,
        previous_client: ClaudeAdapterInstaller,
        previous_receipt: dict[str, object],
    ) -> AdapterInstallResult:
        mutation_started = False
        try:
            mutation_started = True
            previous_client._run(
                ["claude", "plugin", "uninstall", "csaf@csaf", "--scope", "user"],
                "plugin uninstall",
            )
            self._checkpoint("claude:old-plugin-removed")
            previous_client._run(
                ["claude", "plugin", "marketplace", "remove", "csaf"],
                "marketplace remove",
            )
            self._checkpoint("claude:old-marketplace-removed")
            client.install()
            if self._state(client) != (True, True):
                raise SetupError("Claude adapter transition verification failed", activated=True)
            receipt = {
                "schema_version": 1,
                "version": str(version),
                "asset_sha256": asset_sha256,
                "marketplace_owned": True,
                "plugin_owned": True,
            }
            write_json_atomic(self.destination / _CLAUDE_RECEIPT, receipt)
            return AdapterInstallResult(self.kind, self.destination)
        except Exception as error:
            restored = not mutation_started or self._restore_transition(
                client, previous_client, previous_receipt
            )
            raise SetupError(
                "Claude adapter version transition failed", activated=not restored
            ) from error

    def _restore_transition(
        self,
        client: ClaudeAdapterInstaller,
        previous_client: ClaudeAdapterInstaller,
        previous_receipt: dict[str, object],
    ) -> bool:
        try:
            marketplace, plugin = self._state(client)
            if plugin:
                client._run(
                    ["claude", "plugin", "uninstall", "csaf@csaf", "--scope", "user"],
                    "plugin rollback",
                )
            if marketplace:
                client._run(
                    ["claude", "plugin", "marketplace", "remove", "csaf"],
                    "marketplace rollback",
                )
        except SetupError:
            pass
        try:
            previous_client.install()
            if self._state(previous_client) != (True, True):
                return False
            write_json_atomic(self.destination / _CLAUDE_RECEIPT, previous_receipt)
            return True
        except (SetupError, OSError, ValueError):
            return False

    def restore(self, target: Path, version: Version, checksum: str) -> None:
        if Path(target) != self.destination:
            raise SetupError("Claude adapter restore destination is invalid")
        _reject_linked_parents(self.destination)
        receipt = read_json(self.destination / _CLAUDE_RECEIPT)
        parsed = self._versioned_receipt(receipt)
        if (
            parsed is None
            or parsed[0] != version
            or parsed[1].get("asset_sha256") != checksum
            or parsed[1].get("marketplace_owned") is not True
            or parsed[1].get("plugin_owned") is not True
        ):
            raise SetupError("Claude adapter restore receipt is invalid")
        client = self._client(version)
        client.install()
        if self._state(client) != (True, True):
            raise SetupError("Claude adapter restore verification failed", activated=True)

    def health(self, target: Path, version: Version, sha256: str) -> bool:
        if self.has_pending_uninstall():
            return False
        if Path(target) != self.destination:
            return False
        try:
            _reject_linked_parents(self.destination)
            receipt = read_json(self.destination / _CLAUDE_RECEIPT)
            if not (
                isinstance(receipt, dict)
                and receipt.get("schema_version") == 1
                and receipt.get("version") == str(version)
                and receipt.get("asset_sha256") == sha256
            ):
                return False
            return self._state(self._client(version)) == (True, True)
        except Exception:
            return False

    @property
    def _uninstall_journal(self) -> Path:
        return self.destination.parent / ".claude-uninstall.json"

    def has_pending_uninstall(self) -> bool:
        journal = self._uninstall_journal
        return journal.exists() or journal.is_symlink()

    @staticmethod
    def _validate_uninstall_record(value: object) -> dict[str, object]:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "version", "marketplace_owned", "plugin_owned"}
            or value.get("schema_version") != 1
            or not isinstance(value.get("version"), str)
            or type(value.get("marketplace_owned")) is not bool
            or type(value.get("plugin_owned")) is not bool
        ):
            raise SetupError("Claude adapter uninstall journal is invalid")
        try:
            Version(value["version"])
        except ValueError as error:
            raise SetupError("Claude adapter uninstall journal is invalid") from error
        return dict(value)

    def _load_uninstall_record(self) -> dict[str, object]:
        journal = self._uninstall_journal
        _prepare_private_parent(journal.parent)
        _reject_linked_parents(journal)
        if journal.is_file():
            return self._validate_uninstall_record(read_json(journal))
        _reject_linked_parents(self.destination)
        receipt = read_json(self.destination / _CLAUDE_RECEIPT)
        parsed = self._versioned_receipt(receipt)
        if parsed is None:
            raise SetupError("Claude adapter receipt is invalid")
        record = {
            "schema_version": 1,
            "version": str(parsed[0]),
            "marketplace_owned": parsed[1].get("marketplace_owned") is True,
            "plugin_owned": parsed[1].get("plugin_owned") is True,
        }
        self._journal_writer(journal, record)
        return record

    def _checkpoint_uninstall_record(self, record: Mapping[str, object]) -> None:
        value = self._validate_uninstall_record(dict(record))
        _reject_linked_parents(self._uninstall_journal)
        self._journal_writer(self._uninstall_journal, value)
        receipt_path = self.destination / _CLAUDE_RECEIPT
        if receipt_path.is_file():
            receipt = read_json(receipt_path)
            parsed = self._versioned_receipt(receipt)
            if parsed is None or str(parsed[0]) != value["version"]:
                raise SetupError("Claude adapter receipt is invalid")
            updated = dict(parsed[1])
            updated["marketplace_owned"] = value["marketplace_owned"]
            updated["plugin_owned"] = value["plugin_owned"]
            write_json_atomic(receipt_path, updated)

    def finalize_uninstall(self) -> None:
        journal = self._uninstall_journal
        if not (journal.exists() or journal.is_symlink()):
            return
        _reject_linked_parents(journal)
        record = self._validate_uninstall_record(read_json(journal))
        if record["marketplace_owned"] is True or record["plugin_owned"] is True:
            raise SetupError("Claude adapter uninstall is not ready to finalize")
        if self.destination.exists() or self.destination.is_symlink():
            raise SetupError("Claude adapter uninstall target still exists")
        journal.unlink()
        _fsync_directory(journal.parent)

    def uninstall(self, target: Path) -> None:
        if Path(target) != self.destination:
            raise SetupError("Claude adapter destination does not match its receipt")
        mutation_started = False
        try:
            record = self._load_uninstall_record()
            version = Version(str(record["version"]))
            client = self._client(version)
            marketplace, plugin = self._state(client)
            if record["plugin_owned"] is True:
                if plugin:
                    client._run(
                        ["claude", "plugin", "uninstall", "csaf@csaf", "--scope", "user"],
                        "plugin uninstall",
                    )
                    mutation_started = True
                record["plugin_owned"] = False
                self._checkpoint_uninstall_record(record)
            marketplace, plugin = self._state(client)
            if record["marketplace_owned"] is True:
                if plugin:
                    raise SetupError(
                        "Claude adapter cleanup found an unexpected remaining plugin",
                        activated=mutation_started,
                    )
                if marketplace:
                    client._run(
                        ["claude", "plugin", "marketplace", "remove", "csaf"],
                        "marketplace remove",
                    )
                    mutation_started = True
                record["marketplace_owned"] = False
                self._checkpoint_uninstall_record(record)
            if self.destination.exists() or self.destination.is_symlink():
                _reject_linked_parents(self.destination)
                shutil.rmtree(self.destination)
                mutation_started = True
            self._checkpoint("claude:receipt-deleted")
        except SetupError as error:
            raise SetupError(
                "Claude adapter cleanup is incomplete",
                activated=mutation_started or error.activated,
            ) from error
        except Exception as error:
            raise SetupError(
                "Claude adapter cleanup failed",
                activated=mutation_started,
            ) from error


class SetupManager:
    def __init__(
        self,
        *,
        data_root: Path,
        platform: SupportedPlatform,
        detected_assistants: Sequence[AssistantKind],
        downloader: Downloader = download_verified,
        runtime_installer: RuntimeInstaller = _missing_runtime,
        adapter_installers: Mapping[AssistantKind, ManagedAdapter] | None = None,
        doctor_runner: DoctorRunner = _doctor,
        runtime_probe: RuntimeProbe = _runtime_probe,
        officecli_probe: OfficeProbe = _office_probe,
        json_writer: JsonWriter = write_json_atomic,
        clock: Clock | None = None,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        self._data_root = Path(data_root)
        self._platform = platform
        self._detected = tuple(dict.fromkeys(detected_assistants))
        self._downloader = downloader
        self._runtime_installer = runtime_installer
        self._adapters = dict(adapter_installers or {})
        self._doctor_runner = doctor_runner
        self._runtime_probe = runtime_probe
        self._officecli_probe = officecli_probe
        self._json_writer = json_writer
        self._clock = clock or (lambda: datetime.now(UTC))
        self._checkpoint = checkpoint or (lambda _phase: None)

    @contextmanager
    def _setup_lock(self) -> object:
        try:
            _prepare_private_parent(self._data_root)
            _reject_linked_parents(self._data_root)
            descriptor = _acquire_activation_lock(self._data_root / ".setup.lock")
        except SetupError as error:
            raise _SetupBusy("another setup operation is already in progress") from error
        try:
            _reject_linked_parents(self._data_root)
            self._recover_stale_transactions()
            self._recover_pending_uninstalls()
            yield
        finally:
            try:
                _release_activation_lock(descriptor)
            except OSError as error:
                raise SetupError("could not release the setup operation lock") from error

    def _has_pending_uninstalls(self) -> bool:
        for adapter in self._adapters.values():
            checker = getattr(adapter, "has_pending_uninstall", None)
            if not callable(checker):
                continue
            try:
                if checker() is True:
                    return True
            except Exception:
                return True
        return False

    def _recover_pending_uninstalls(self) -> None:
        state = self._read_state()
        for kind, adapter in self._adapters.items():
            checker = getattr(adapter, "has_pending_uninstall", None)
            if not callable(checker) or checker() is not True:
                continue
            adapter.uninstall(adapter.destination)
            if state is not None and kind in state.adapter_targets:
                remaining = dict(state.adapter_targets)
                remaining.pop(kind, None)
                self._write_uninstall_state(state, remaining)
                state = self._read_state()
                if state is None:
                    raise SetupError("pending uninstall state checkpoint could not be verified")
            finalizer = getattr(adapter, "finalize_uninstall", None)
            if not callable(finalizer):
                raise SetupError("pending uninstall finalizer is unavailable")
            finalizer()

    def plan_install(
        self, manifest: ReleaseManifest, *, requested_targets: Sequence[AssistantKind] | None
    ) -> InstallPlan:
        self._policy(manifest)
        targets = self._select_targets(requested_targets)
        state = self._read_state()
        if (
            requested_targets is not None
            and state is not None
            and state.active_version is not None
            and manifest.version > state.active_version
            and not set(state.adapter_targets) <= set(targets)
        ):
            raise SetupError("updates must include every installed assistant")
        runtime = self._data_root / "versions" / str(manifest.version)
        office_name = (
            "officecli.exe" if self._platform.value.startswith("windows-") else "officecli"
        )
        office = self._data_root / "officecli" / str(_OFFICE_VERSION) / office_name
        assets = {
            kind: manifest.claude_plugin if kind is AssistantKind.CLAUDE else manifest.codex_skill
            for kind in targets
        }
        destinations: dict[AssistantKind, Path] = {}
        for kind in targets:
            adapter = self._adapters.get(kind)
            if adapter is None:
                raise SetupError(f"{kind.value} adapter installer is unavailable")
            try:
                destinations[kind] = Path(adapter.destination)
            except Exception as error:
                raise SetupError("assistant adapter configuration is invalid") from error
        values = dict(
            manifest=manifest,
            platform=self._platform,
            targets=targets,
            data_root=self._data_root,
            runtime_asset=manifest.runtime[self._platform],
            officecli_asset=manifest.officecli.assets[self._platform],
            adapter_assets=MappingProxyType(assets),
            adapter_destinations=MappingProxyType(destinations),
            runtime_path=runtime,
            officecli_path=office,
        )
        plan = InstallPlan(**values)
        return InstallPlan(**values, already_healthy=self._health(plan).ready)

    def install(
        self, plan: InstallPlan, *, consent: Consent, assume_yes: bool = False, force: bool = False
    ) -> SetupResult:
        self._validate(plan)
        old_version = self._active_version()
        if self._health(plan).ready:
            return SetupResult(SetupStatus.READY, plan.manifest.version, True)
        if not assume_yes:
            try:
                approved = consent(plan)
            except Exception:
                return SetupResult(
                    SetupStatus.FAILED,
                    old_version,
                    False,
                    "setup consent failed",
                )
            if not approved:
                return SetupResult(SetupStatus.CANCELLED, old_version, False)
        try:
            with self._setup_lock():
                return self._install_unlocked(
                    plan,
                    consent=lambda _plan: True,
                    assume_yes=True,
                    force=force,
                )
        except _SetupBusy as error:
            return SetupResult(SetupStatus.FAILED, old_version, False, str(error))
        except SetupError:
            return SetupResult(
                SetupStatus.PARTIAL,
                self._active_version(),
                self._active_version() is not None,
                "setup operation lock failed",
            )

    def _install_unlocked(
        self, plan: InstallPlan, *, consent: Consent, assume_yes: bool = False, force: bool = False
    ) -> SetupResult:
        del force
        self._validate(plan)
        old_state = self._read_state()
        old_version = self._active_version()
        health = self._health(plan)
        if health.ready:
            return SetupResult(SetupStatus.READY, plan.manifest.version, True)
        if not assume_yes:
            try:
                approved = consent(plan)
            except Exception:
                return SetupResult(
                    SetupStatus.FAILED,
                    old_version,
                    False,
                    "setup consent failed",
                )
            if not approved:
                return SetupResult(SetupStatus.CANCELLED, old_version, False)
        need_runtime = not health.runtime
        need_office = not health.officecli
        need_adapters = tuple(kind for kind in plan.targets if not health.adapters[kind])
        transaction = self._data_root / ".staging" / uuid.uuid4().hex
        runtime_backup = transaction / "previous-runtime"
        office_backup = transaction / "previous-officecli"
        state_snapshot = self._snapshot(self._data_root / "state.json")
        current_snapshot = self._snapshot(self._data_root / "current.json")
        changed: dict[AssistantKind, Path] = {}
        adapter_backups: dict[AssistantKind, _AdapterBackup] = {}
        runtime_activated = office_activated = False
        runtime_content: str | None = None
        cleanup_transaction = False
        try:
            self._prepare(transaction)
            for kind in need_adapters:
                adapter_backups[kind] = self._snapshot_adapter(
                    kind, old_state, transaction, plan.manifest.version
                )
            self._write_transaction_journal(
                transaction,
                plan,
                state_snapshot=state_snapshot,
                current_snapshot=current_snapshot,
                adapter_backups=adapter_backups,
            )
            candidate_runtime = plan.runtime_path
            candidate_office = plan.officecli_path
            if need_runtime:
                wheel = transaction / "runtime.whl"
                self._download(plan.runtime_asset, wheel)
                try:
                    installed = self._runtime_installer(
                        wheel, transaction / "runtime", plan.manifest.version
                    )
                except Exception as error:
                    raise SetupError("runtime staging failed") from error
                candidate_runtime = self._staged_runtime(installed, transaction)
                self._secure_runtime(candidate_runtime)
                (candidate_runtime / _MARKER).write_text(
                    plan.runtime_asset.sha256 + "\n", encoding="ascii"
                )
                runtime_content = self._tree_digest(candidate_runtime)
            if need_office:
                candidate_office = transaction / plan.officecli_path.name
                self._download(plan.officecli_asset, candidate_office)
                if os.name != "nt":
                    os.chmod(candidate_office, 0o700)
            for kind in need_adapters:
                asset_path = transaction / f"{kind.value}.adapter"
                self._download(plan.adapter_assets[kind], asset_path)
                self._set_journal_adapter_phase(transaction, kind, "mutating")
                try:
                    result = self._adapters[kind].install(asset_path, plan.manifest.version)
                except SetupError as error:
                    if error.activated:
                        changed[kind] = plan.adapter_destinations[kind]
                    if isinstance(error, _ClaudeVersionConflict):
                        raise
                    raise SetupError(
                        "assistant adapter installation failed", activated=error.activated
                    ) from error
                except Exception as error:
                    raise SetupError("assistant adapter installation failed") from error
                destination = Path(result.target or "")
                if result.kind is not kind or destination != plan.adapter_destinations[kind]:
                    changed[kind] = plan.adapter_destinations[kind]
                    raise SetupError(
                        "assistant adapter installation returned an unexpected destination",
                        activated=True,
                    )
                changed[kind] = destination
                self._set_journal_adapter_phase(transaction, kind, "mutated")
                self._checkpoint(f"adapter:{kind.value}:mutated")
            try:
                ready = self._doctor_runner(
                    candidate_runtime, candidate_office, self._office_environment(candidate_office)
                )
            except Exception as error:
                raise SetupError("setup diagnostics failed") from error
            if ready is not True:
                raise SetupError("setup diagnostics failed")
            if need_runtime:
                self._activate(candidate_runtime, plan.runtime_path, runtime_backup)
                runtime_activated = True
            if need_office:
                self._activate(candidate_office, plan.officecli_path, office_backup)
                office_activated = True
            state = self._next_state(plan, old_state, changed, runtime_content)
            try:
                self._json_writer(self._data_root / "state.json", state.model_dump(mode="json"))
            except Exception as error:
                raise SetupError("could not activate installation state") from error
            if self._state_healthy(state, require_current=False) is not True:
                raise SetupError("activated installation failed health verification")
            try:
                self._json_writer(
                    self._data_root / "current.json",
                    {
                        "schema_version": 1,
                        "active_version": str(plan.manifest.version),
                        "runtime_path": str(plan.runtime_path),
                    },
                )
                self._checkpoint("current-replaced")
                write_json_atomic(
                    transaction / "committed.json",
                    {
                        "schema_version": 1,
                        "version": str(plan.manifest.version),
                    },
                )
                self._checkpoint("committed")
            except Exception as error:
                raise SetupError("could not activate installation state") from error
            cleanup_transaction = True
            return SetupResult(SetupStatus.READY, plan.manifest.version, True)
        except SetupError as error:
            files_restored = self._recover_files(
                runtime=plan.runtime_path,
                runtime_backup=runtime_backup,
                runtime_activated=runtime_activated,
                officecli=plan.officecli_path,
                office_backup=office_backup,
                office_activated=office_activated,
                state_snapshot=state_snapshot,
                current_snapshot=current_snapshot,
            )
            remaining = self._restore_changed_adapters(changed, adapter_backups)
            if remaining:
                self._persist_partial(old_state, plan, remaining)
            partial = bool(remaining) or not files_restored
            cleanup_transaction = not partial
            return SetupResult(
                SetupStatus.PARTIAL if partial else SetupStatus.FAILED,
                old_version,
                False,
                "installation rollback is incomplete" if not files_restored else str(error),
            )
        except Exception:
            files_restored = self._recover_files(
                runtime=plan.runtime_path,
                runtime_backup=runtime_backup,
                runtime_activated=runtime_activated,
                officecli=plan.officecli_path,
                office_backup=office_backup,
                office_activated=office_activated,
                state_snapshot=state_snapshot,
                current_snapshot=current_snapshot,
            )
            remaining = self._restore_changed_adapters(changed, adapter_backups)
            if remaining:
                self._persist_partial(old_state, plan, remaining)
            partial = bool(remaining) or not files_restored
            cleanup_transaction = not partial
            return SetupResult(
                SetupStatus.PARTIAL if partial else SetupStatus.FAILED,
                old_version,
                False,
                "installation rollback is incomplete"
                if not files_restored
                else "native setup failed",
            )
        finally:
            if cleanup_transaction:
                self._cleanup(transaction)

    def repair(
        self, plan: InstallPlan, *, consent: Consent, assume_yes: bool = False
    ) -> SetupResult:
        fresh = self.plan_install(plan.manifest, requested_targets=plan.targets)
        return self.install(fresh, consent=consent, assume_yes=assume_yes)

    def check_update(self, manifest: ReleaseManifest) -> bool:
        self._policy(manifest)
        active = self._active_version()
        return active is None or active < manifest.version

    def update(
        self, plan: InstallPlan, *, consent: Consent, assume_yes: bool = False
    ) -> SetupResult:
        if not self.check_update(plan.manifest) and not self._has_pending_uninstalls():
            return SetupResult(SetupStatus.READY, self._active_version(), True)
        return self.install(plan, consent=consent, assume_yes=assume_yes)

    def doctor(self) -> bool:
        if (
            not (self._data_root / "state.json").is_file()
            and not (self._data_root / ".staging").exists()
            and not self._has_pending_uninstalls()
        ):
            return False
        try:
            with self._setup_lock():
                return self._doctor_unlocked()
        except SetupError:
            return False

    def _doctor_unlocked(self) -> bool:
        state = self._read_state()
        if state is None or state.active_version is None:
            return False
        return self._state_healthy(state, require_current=True)

    def _state_healthy(self, state: InstallState, *, require_current: bool) -> bool:
        if state.active_version is None:
            return False
        runtime = state.runtime_paths.get(state.active_version)
        office = state.officecli_path
        if (
            runtime is None
            or office is None
            or (require_current and not self._current_points(state.active_version, runtime))
        ):
            return False
        content = state.verified_checksums.get(f"runtime-content:{state.active_version}")
        if (
            not content
            or self._tree_digest(runtime) != content
            or not self._runtime_permissions(runtime)
        ):
            return False
        try:
            if self._runtime_probe(runtime, state.active_version) is not True:
                return False
        except Exception:
            return False
        if state.officecli_version != _OFFICE_VERSION or state.officecli_sha256 is None:
            return False
        if not self._matches_digest(
            office, state.officecli_sha256
        ) or not self._executable_permissions(office):
            return False
        try:
            if self._officecli_probe(office, _OFFICE_VERSION, _OFFICE_MINIMUM) is not True:
                return False
        except Exception:
            return False
        for kind, target in state.adapter_targets.items():
            adapter = self._adapters.get(kind)
            version, checksum = self._adapter_record(state, kind)
            if (
                adapter is None
                or version is None
                or version != state.active_version
                or checksum is None
                or not self._path_permissions(target)
            ):
                return False
            try:
                if adapter.health(target, version, checksum) is not True:
                    return False
            except Exception:
                return False
        try:
            return self._doctor_runner(runtime, office, self._office_environment(office)) is True
        except Exception:
            return False

    def uninstall(
        self,
        *,
        consent: Callable[[], bool],
        include_officecli: bool = False,
        assume_yes: bool = False,
    ) -> SetupResult:
        active = self._active_version()
        if not assume_yes:
            try:
                approved = consent()
            except Exception:
                return SetupResult(
                    SetupStatus.FAILED,
                    active,
                    active is not None,
                    "setup consent failed",
                )
            if not approved:
                return SetupResult(SetupStatus.CANCELLED, active, active is not None)
        if self._read_state() is None and not self._has_pending_uninstalls():
            return SetupResult(SetupStatus.READY, None, False)
        try:
            with self._setup_lock():
                return self._uninstall_unlocked(
                    consent=lambda: True,
                    include_officecli=include_officecli,
                    assume_yes=True,
                )
        except _SetupBusy as error:
            return SetupResult(SetupStatus.FAILED, active, active is not None, str(error))
        except SetupError:
            return SetupResult(
                SetupStatus.PARTIAL,
                self._active_version(),
                self._active_version() is not None,
                "setup operation lock failed",
            )

    def _uninstall_unlocked(
        self,
        *,
        consent: Callable[[], bool],
        include_officecli: bool = False,
        assume_yes: bool = False,
    ) -> SetupResult:
        state = self._read_state()
        active = self._active_version()
        if not assume_yes:
            try:
                approved = consent()
            except Exception:
                return SetupResult(
                    SetupStatus.FAILED,
                    active,
                    active is not None,
                    "setup consent failed",
                )
            if not approved:
                return SetupResult(SetupStatus.CANCELLED, active, active is not None)
        if state is None:
            return SetupResult(SetupStatus.READY, None, False)
        remaining = dict(state.adapter_targets)
        failed = False
        for kind, target in tuple(state.adapter_targets.items()):
            adapter = self._adapters.get(kind)
            if adapter is None:
                failed = True
                continue
            try:
                adapter.uninstall(target)
            except Exception:
                failed = True
            else:
                remaining.pop(kind, None)
                try:
                    self._write_uninstall_state(state, remaining)
                    finalizer = getattr(adapter, "finalize_uninstall", None)
                    if callable(finalizer):
                        finalizer()
                except Exception:
                    failed = True
                    break
        if failed:
            try:
                self._write_uninstall_state(state, remaining)
            except Exception:
                pass
            return SetupResult(
                SetupStatus.PARTIAL,
                active,
                active is not None,
                "native adapter cleanup is incomplete",
            )
        try:
            self._remove_owned(self._data_root / "versions")
            self._remove_owned(self._data_root / "current.json")
            if include_officecli and state.officecli_installed_by_csaf:
                self._remove_owned(self._data_root / "officecli")
                self._remove_owned(self._data_root / "state.json")
            elif state.officecli_installed_by_csaf:
                retained = state.model_copy(
                    update={
                        "active_version": None,
                        "installed_versions": (),
                        "runtime_paths": {},
                        "adapter_targets": {},
                    }
                )
                write_json_atomic(self._data_root / "state.json", retained.model_dump(mode="json"))
            else:
                self._remove_owned(self._data_root / "state.json")
        except Exception:
            selected = self._active_version()
            active_runtime = state.runtime_paths.get(selected) if selected is not None else None
            if active_runtime is None or not active_runtime.is_dir():
                try:
                    self._restore(self._data_root / "current.json", None)
                    surviving = {
                        version: path
                        for version, path in state.runtime_paths.items()
                        if path.is_dir()
                    }
                    reconciled = state.model_copy(
                        update={
                            "active_version": None,
                            "installed_versions": tuple(surviving),
                            "runtime_paths": surviving,
                            "adapter_targets": {},
                        }
                    )
                    write_json_atomic(
                        self._data_root / "state.json",
                        reconciled.model_dump(mode="json"),
                    )
                except Exception:
                    pass
                selected = None
            return SetupResult(
                SetupStatus.PARTIAL,
                selected,
                selected is not None,
                "native setup cleanup is incomplete",
            )
        return SetupResult(SetupStatus.READY, None, False)

    def _health(self, plan: InstallPlan) -> _Health:
        state = self._read_state()
        runtime = self._runtime_health(plan, state)
        office = self._office_health(plan, state)
        values: dict[AssistantKind, bool] = {}
        for kind in plan.targets:
            healthy = False
            if state is not None:
                target = state.adapter_targets.get(kind)
                checksum = state.verified_checksums.get(
                    f"adapter:{kind.value}:{plan.manifest.version}"
                )
                adapter = self._adapters.get(kind)
                if (
                    target == plan.adapter_destinations[kind]
                    and checksum == plan.adapter_assets[kind].sha256
                ):
                    try:
                        healthy = bool(
                            adapter
                            and adapter.health(target, plan.manifest.version, checksum) is True
                            and self._path_permissions(target)
                        )
                    except Exception:
                        healthy = False
            values[kind] = healthy
        return _Health(runtime, office, MappingProxyType(values))

    def _runtime_health(self, plan: InstallPlan, state: InstallState | None) -> bool:
        if state is None or state.active_version != plan.manifest.version:
            return False
        if state.runtime_paths.get(
            plan.manifest.version
        ) != plan.runtime_path or not self._current_points(
            plan.manifest.version, plan.runtime_path
        ):
            return False
        try:
            if (plan.runtime_path / _MARKER).read_text(
                encoding="ascii"
            ).strip() != plan.runtime_asset.sha256:
                return False
        except (OSError, UnicodeError):
            return False
        content = state.verified_checksums.get(f"runtime-content:{plan.manifest.version}")
        if (
            not content
            or self._tree_digest(plan.runtime_path) != content
            or not self._runtime_permissions(plan.runtime_path)
        ):
            return False
        try:
            return self._runtime_probe(plan.runtime_path, plan.manifest.version) is True
        except Exception:
            return False

    def _office_health(self, plan: InstallPlan, state: InstallState | None) -> bool:
        if (
            state is None
            or state.officecli_version != _OFFICE_VERSION
            or state.officecli_path != plan.officecli_path
            or state.officecli_sha256 != plan.officecli_asset.sha256
        ):
            return False
        if not self._matches_asset(
            plan.officecli_path, plan.officecli_asset
        ) or not self._executable_permissions(plan.officecli_path):
            return False
        try:
            return (
                self._officecli_probe(plan.officecli_path, _OFFICE_VERSION, _OFFICE_MINIMUM) is True
            )
        except Exception:
            return False

    def _next_state(
        self,
        plan: InstallPlan,
        previous: InstallState | None,
        changed: Mapping[AssistantKind, Path],
        runtime_content: str | None,
    ) -> InstallState:
        installed = list(previous.installed_versions if previous else ())
        if plan.manifest.version not in installed:
            installed.append(plan.manifest.version)
        runtimes = dict(previous.runtime_paths if previous else {})
        runtimes[plan.manifest.version] = plan.runtime_path
        targets = dict(previous.adapter_targets if previous else {})
        targets.update(changed)
        checksums = dict(previous.verified_checksums if previous else {})
        for kind in changed:
            prefix = f"adapter:{kind.value}:"
            checksums = {
                key: value for key, value in checksums.items() if not key.startswith(prefix)
            }
        checksums[f"runtime:{plan.manifest.version}"] = plan.runtime_asset.sha256
        checksums[f"runtime-content:{plan.manifest.version}"] = (
            runtime_content or self._tree_digest(plan.runtime_path)
        )
        checksums[f"officecli:{_OFFICE_VERSION}"] = plan.officecli_asset.sha256
        for kind in plan.targets:
            if kind in targets:
                checksums[f"adapter:{kind.value}:{plan.manifest.version}"] = plan.adapter_assets[
                    kind
                ].sha256
        now = self._now()
        return InstallState(
            active_version=plan.manifest.version,
            installed_versions=tuple(installed),
            runtime_paths=runtimes,
            verified_checksums=checksums,
            adapter_targets=targets,
            officecli_version=_OFFICE_VERSION,
            officecli_path=plan.officecli_path,
            officecli_sha256=plan.officecli_asset.sha256,
            officecli_installed_by_csaf=True,
            installed_at=(previous.installed_at if previous else None) or now,
            updated_at=now,
        )

    def _snapshot_adapter(
        self,
        kind: AssistantKind,
        state: InstallState | None,
        transaction: Path,
        installing_version: Version,
    ) -> _AdapterBackup:
        adapter = self._adapters[kind]
        snapshotter = getattr(adapter, "recovery_snapshot", None)
        recovery: Mapping[str, object] = MappingProxyType({})
        if callable(snapshotter):
            raw_recovery = snapshotter(installing_version)
            if not isinstance(raw_recovery, Mapping):
                raise SetupError("assistant adapter recovery snapshot is invalid")
            recovery = MappingProxyType(dict(raw_recovery))
        target = adapter.destination
        fresh_target = not (target.exists() or target.is_symlink())
        empty = _AdapterBackup(
            kind,
            target,
            None,
            None,
            None,
            fresh_target,
            MappingProxyType({}),
            recovery,
        )
        if state is None or state.adapter_targets.get(kind) != target:
            return empty
        version, checksum = self._adapter_record(state, kind)
        adapter = self._adapters[kind]
        if version is None or checksum is None:
            return empty
        try:
            healthy = adapter.health(target, version, checksum) is True and self._path_permissions(
                target
            )
        except Exception:
            healthy = False
        if not healthy:
            return _AdapterBackup(
                kind,
                target,
                None,
                version,
                checksum,
                False,
                MappingProxyType({}),
                recovery,
            )
        backup = transaction / f"previous-adapter-{kind.value}"
        self._copy_adapter_tree(target, backup)
        ownership: dict[str, bool] = {}
        if isinstance(adapter, ClaudeManagedAdapter):
            receipt = read_json(backup / _CLAUDE_RECEIPT)
            if isinstance(receipt, dict):
                ownership = {
                    "marketplace_owned": receipt.get("marketplace_owned") is True,
                    "plugin_owned": receipt.get("plugin_owned") is True,
                }
        else:
            ownership = {"skill_owned": True}
        return _AdapterBackup(
            kind,
            target,
            backup,
            version,
            checksum,
            False,
            MappingProxyType(ownership),
            recovery,
        )

    @staticmethod
    def _copy_adapter_tree(source: Path, destination: Path) -> None:
        _reject_linked_parents(source)
        _reject_linked_parents(destination.parent)
        for directory, names, files in os.walk(source, followlinks=False):
            for name in (*names, *files):
                details = (Path(directory) / name).lstat()
                attributes = getattr(details, "st_file_attributes", 0)
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if stat.S_ISLNK(details.st_mode) or attributes & reparse_flag:
                    raise SetupError("adapter snapshot contains a symlink or reparse point")
        shutil.copytree(source, destination, copy_function=shutil.copy2)
        if os.name != "nt":
            for item in (destination, *destination.rglob("*")):
                os.chmod(item, 0o700 if item.is_dir() else 0o600)

    def _restore_changed_adapters(
        self,
        changed: Mapping[AssistantKind, Path],
        backups: Mapping[AssistantKind, _AdapterBackup],
    ) -> dict[AssistantKind, Path]:
        remaining: dict[AssistantKind, Path] = {}
        for kind, target in reversed(tuple(changed.items())):
            adapter = self._adapters.get(kind)
            backup = backups.get(kind)
            if adapter is None or backup is None:
                remaining[kind] = target
                continue
            try:
                reconciler = getattr(adapter, "reconcile", None)
                reconciled = callable(reconciler)
                if reconciled:
                    reconciler(backup.recovery)
                elif backup.backup is None and not backup.fresh_target:
                    if (
                        backup.version is not None
                        and backup.checksum is not None
                        and adapter.health(target, backup.version, backup.checksum) is True
                    ):
                        continue
                    raise SetupError("adapter target ownership could not be proven")
                else:
                    adapter.uninstall(target)
                if backup.backup is None:
                    if target.exists() or target.is_symlink():
                        raise SetupError("new adapter cleanup could not be verified")
                    continue
                if target.exists() or target.is_symlink():
                    raise SetupError("adapter target remained after cleanup")
                _reject_linked_parents(target.parent)
                self._copy_adapter_tree(backup.backup, target)
                restorer = getattr(adapter, "restore", None)
                if callable(restorer):
                    restorer(target, backup.version, backup.checksum)
                if (
                    backup.version is None
                    or backup.checksum is None
                    or adapter.health(target, backup.version, backup.checksum) is not True
                ):
                    raise SetupError("previous adapter restoration could not be verified")
            except Exception:
                remaining[kind] = target
        return remaining

    def _persist_partial(
        self,
        previous: InstallState | None,
        plan: InstallPlan,
        changed: Mapping[AssistantKind, Path],
    ) -> bool:
        targets = dict(previous.adapter_targets if previous else {})
        targets.update(changed)
        checksums = dict(previous.verified_checksums if previous else {})
        for kind in changed:
            prefix = f"adapter:{kind.value}:"
            checksums = {
                key: value for key, value in checksums.items() if not key.startswith(prefix)
            }
            checksums[f"adapter:{kind.value}:{plan.manifest.version}"] = plan.adapter_assets[
                kind
            ].sha256
        partial = InstallState(
            active_version=previous.active_version if previous else None,
            installed_versions=previous.installed_versions if previous else (),
            runtime_paths=previous.runtime_paths if previous else {},
            verified_checksums=checksums,
            adapter_targets=targets,
            officecli_version=previous.officecli_version if previous else None,
            officecli_path=previous.officecli_path if previous else None,
            officecli_sha256=previous.officecli_sha256 if previous else None,
            officecli_installed_by_csaf=previous.officecli_installed_by_csaf if previous else False,
            installed_at=previous.installed_at if previous else None,
            updated_at=self._now(),
        )
        try:
            write_json_atomic(self._data_root / "state.json", partial.model_dump(mode="json"))
        except Exception:
            return False
        return True

    def _rollback_adapters(
        self,
        changed: Mapping[AssistantKind, Path],
    ) -> dict[AssistantKind, Path]:
        remaining: dict[AssistantKind, Path] = {}
        for kind, target in changed.items():
            adapter = self._adapters.get(kind)
            if adapter is None:
                remaining[kind] = target
                continue
            try:
                adapter.uninstall(target)
            except Exception:
                remaining[kind] = target
        return remaining

    def _write_uninstall_state(
        self, state: InstallState, remaining: Mapping[AssistantKind, Path]
    ) -> None:
        checksums = {
            key: value
            for key, value in state.verified_checksums.items()
            if not key.startswith("adapter:")
            or any(key.startswith(f"adapter:{kind.value}:") for kind in remaining)
        }
        value = state.model_copy(
            update={"adapter_targets": dict(remaining), "verified_checksums": checksums}
        )
        write_json_atomic(self._data_root / "state.json", value.model_dump(mode="json"))

    def _validate(self, plan: InstallPlan) -> None:
        self._policy(plan.manifest)
        if plan.data_root != self._data_root or plan.platform is not self._platform:
            raise SetupError("installation plan does not belong to this setup manager")
        expected = self.plan_install(plan.manifest, requested_targets=plan.targets)
        if (
            plan.runtime_path != expected.runtime_path
            or plan.officecli_path != expected.officecli_path
            or dict(plan.adapter_assets) != dict(expected.adapter_assets)
            or dict(plan.adapter_destinations) != dict(expected.adapter_destinations)
            or plan.runtime_asset != expected.runtime_asset
            or plan.officecli_asset != expected.officecli_asset
        ):
            raise SetupError("installation plan destinations are invalid")

    @staticmethod
    def _policy(manifest: ReleaseManifest) -> None:
        if (
            manifest.officecli.version != _OFFICE_VERSION
            or manifest.officecli.minimum_version < _OFFICE_MINIMUM
        ):
            raise SetupError(
                "OfficeCLI release policy requires version 1.0.143 and minimum 1.0.137"
            )

    def _select_targets(
        self, requested: Sequence[AssistantKind] | None
    ) -> tuple[AssistantKind, ...]:
        detected = set(self._detected)
        if requested is None:
            return tuple(kind for kind in AssistantKind if kind in detected)
        selected = set(requested)
        if not selected <= detected:
            raise SetupError("requested assistant was not detected")
        return tuple(kind for kind in AssistantKind if kind in selected)

    def _download(self, asset: ReleaseAsset, destination: Path) -> None:
        try:
            value = Path(self._downloader(asset, destination))
        except Exception as error:
            raise SetupError("verified asset download failed") from error
        if value != destination or not self._matches_asset(value, asset):
            raise SetupError("verified asset failed manifest verification")

    def _adapter_record(
        self, state: InstallState, kind: AssistantKind
    ) -> tuple[Version | None, str | None]:
        prefix = f"adapter:{kind.value}:"
        for key, checksum in reversed(tuple(state.verified_checksums.items())):
            if key.startswith(prefix):
                try:
                    return Version(key.removeprefix(prefix)), checksum
                except ValueError:
                    return None, None
        return None, None

    def _now(self) -> datetime:
        try:
            return self._clock()
        except Exception:
            return datetime.now(UTC)

    def _read_state(self) -> InstallState | None:
        path = self._data_root / "state.json"
        if not path.is_file():
            return None
        try:
            return InstallState.model_validate(read_json(path))
        except Exception:
            return None

    def _active_version(self) -> Version | None:
        path = self._data_root / "current.json"
        if not path.is_file():
            return None
        try:
            value = read_json(path)
            raw = value.get("active_version") if isinstance(value, dict) else None
            return Version(raw) if isinstance(raw, str) else None
        except Exception:
            return None

    def _current_points(self, version: Version, runtime: Path) -> bool:
        try:
            value = read_json(self._data_root / "current.json")
        except Exception:
            return False
        return (
            isinstance(value, dict)
            and value.get("schema_version") == 1
            and value.get("active_version") == str(version)
            and value.get("runtime_path") == str(runtime)
        )

    @staticmethod
    def _office_environment(path: Path) -> dict[str, str]:
        return {
            "CSAF_OFFICECLI": str(path),
            "OFFICECLI_SKIP_UPDATE": "1",
            "OFFICECLI_RESIDENT_FLUSH": "each",
        }

    @staticmethod
    def _matches_asset(path: Path, asset: ReleaseAsset) -> bool:
        try:
            return (
                path.is_file()
                and path.stat().st_size == asset.size
                and SetupManager._matches_digest(path, asset.sha256)
            )
        except OSError:
            return False

    @staticmethod
    def _matches_digest(path: Path, expected: str) -> bool:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source:
                while chunk := source.read(65536):
                    digest.update(chunk)
        except OSError:
            return False
        return hmac.compare_digest(digest.hexdigest(), expected)

    @staticmethod
    def _tree_digest(root: Path) -> str:
        digest = hashlib.sha256()
        try:
            files = sorted(
                path for path in root.rglob("*") if path.is_file() and path.name != _MARKER
            )
            for path in files:
                relative = path.relative_to(root).as_posix().encode()
                digest.update(len(relative).to_bytes(4, "big"))
                digest.update(relative)
                with path.open("rb") as source:
                    while chunk := source.read(65536):
                        digest.update(chunk)
        except OSError:
            return ""
        return digest.hexdigest()

    @staticmethod
    def _executable_permissions(path: Path) -> bool:
        if not path.is_file():
            return False
        if os.name == "nt":
            return True
        mode = stat.S_IMODE(path.stat().st_mode)
        return bool(mode & 0o100) and not bool(mode & 0o077)

    @classmethod
    def _runtime_permissions(cls, root: Path) -> bool:
        launcher = root / ("csaf.exe" if os.name == "nt" else "csaf")
        return (
            root.is_dir() and cls._path_permissions(root) and cls._executable_permissions(launcher)
        )

    @staticmethod
    def _path_permissions(path: Path) -> bool:
        if not path.exists():
            return False
        if os.name == "nt":
            return True
        try:
            return all(
                not stat.S_IMODE(item.stat().st_mode) & 0o077 for item in (path, *path.rglob("*"))
            )
        except OSError:
            return False

    @staticmethod
    def _secure_runtime(root: Path) -> None:
        if os.name == "nt":
            return
        launcher = root / "csaf"
        for item in (root, *root.rglob("*")):
            os.chmod(item, 0o700 if item.is_dir() or item == launcher else 0o600)

    def _staged_runtime(self, runtime: Path, transaction: Path) -> Path:
        value = Path(runtime)
        launcher_name = "csaf.exe" if os.name == "nt" else "csaf"
        root = value.parent if value.is_file() and value.name == launcher_name else value
        try:
            root.relative_to(transaction)
        except ValueError as error:
            raise SetupError("runtime installer returned an unsafe path") from error
        _reject_linked_parents(root)
        if not root.is_dir() or not (root / launcher_name).is_file():
            raise SetupError("staged runtime is incomplete")
        return root

    def _write_transaction_journal(
        self,
        transaction: Path,
        plan: InstallPlan,
        *,
        state_snapshot: bytes | None,
        current_snapshot: bytes | None,
        adapter_backups: Mapping[AssistantKind, _AdapterBackup],
    ) -> None:
        if state_snapshot is not None:
            write_json_atomic(
                transaction / "previous-state.json",
                json.loads(state_snapshot.decode("utf-8", errors="strict")),
            )
        if current_snapshot is not None:
            write_json_atomic(
                transaction / "previous-current.json",
                json.loads(current_snapshot.decode("utf-8", errors="strict")),
            )
        adapters = {
            kind.value: {
                "target": str(backup.target),
                "version": str(backup.version) if backup.version is not None else None,
                "checksum": backup.checksum,
                "backup": backup.backup.name if backup.backup is not None else None,
                "backup_present": backup.backup is not None,
                "fresh_target": backup.fresh_target,
                "ownership": dict(backup.ownership),
                "recovery": dict(backup.recovery),
                "phase": "prepared",
            }
            for kind, backup in adapter_backups.items()
        }
        write_json_atomic(
            transaction / "transaction.json",
            {
                "schema_version": 1,
                "version": str(plan.manifest.version),
                "runtime_path": str(plan.runtime_path),
                "officecli_path": str(plan.officecli_path),
                "state_present": state_snapshot is not None,
                "current_present": current_snapshot is not None,
                "adapters": adapters,
            },
        )

    @staticmethod
    def _set_journal_adapter_phase(
        transaction: Path,
        kind: AssistantKind,
        phase: str,
    ) -> None:
        journal_path = transaction / "transaction.json"
        journal = read_json(journal_path)
        if not isinstance(journal, dict) or not isinstance(journal.get("adapters"), dict):
            raise SetupError("setup transaction journal is invalid")
        adapter = journal["adapters"].get(kind.value)
        if not isinstance(adapter, dict) or phase not in {"mutating", "mutated"}:
            raise SetupError("setup transaction journal is invalid")
        adapter["phase"] = phase
        write_json_atomic(journal_path, journal)

    def _recover_stale_transactions(self) -> None:
        staging = self._data_root / ".staging"
        if not staging.exists():
            return
        _reject_linked_parents(staging)
        for transaction in tuple(staging.iterdir()):
            journal_path = transaction / "transaction.json"
            try:
                _reject_linked_parents(transaction)
            except SetupError:
                self._remove_owned(transaction)
                continue
            if not transaction.is_dir() or not journal_path.is_file():
                self._remove_owned(transaction)
                continue
            _reject_linked_parents(journal_path)
            journal = read_json(journal_path)
            if not isinstance(journal, dict) or journal.get("schema_version") != 1:
                raise SetupError("stale setup transaction is invalid")
            raw_version = journal.get("version")
            raw_runtime = journal.get("runtime_path")
            raw_office = journal.get("officecli_path")
            if not all(isinstance(value, str) for value in (raw_version, raw_runtime, raw_office)):
                raise SetupError("stale setup transaction is invalid")
            try:
                version = Version(raw_version)
            except ValueError as error:
                raise SetupError("stale setup transaction is invalid") from error
            runtime = Path(raw_runtime)
            office = Path(raw_office)
            self._require_owned_target(runtime)
            self._require_owned_target(office)
            committed = self._journal_commit_is_healthy(transaction, version, runtime)
            if committed:
                self._remove_owned(transaction)
                continue

            changed, backups = self._journal_adapter_recovery(transaction, journal)
            remaining = self._restore_changed_adapters(changed, backups)
            self._restore_component_backup(
                runtime,
                transaction / "previous-runtime",
            )
            self._restore_component_backup(
                office,
                transaction / "previous-officecli",
            )
            state_backup = transaction / "previous-state.json"
            current_backup = transaction / "previous-current.json"
            if journal.get("state_present") is True:
                _reject_linked_parents(state_backup)
                state_content = state_backup.read_bytes()
            else:
                state_content = None
            if journal.get("current_present") is True:
                _reject_linked_parents(current_backup)
                current_content = current_backup.read_bytes()
            else:
                current_content = None
            self._restore(self._data_root / "state.json", state_content)
            self._restore(self._data_root / "current.json", current_content)
            if remaining:
                raise SetupError("stale adapter recovery is incomplete")
            if state_content is not None and self._doctor_unlocked() is not True:
                raise SetupError("stale setup recovery could not verify the prior installation")
            self._remove_owned(transaction)

    def _journal_commit_is_healthy(
        self,
        transaction: Path,
        version: Version,
        runtime: Path,
    ) -> bool:
        marker = transaction / "committed.json"
        if not marker.is_file():
            return False
        _reject_linked_parents(marker)
        value = read_json(marker)
        return (
            isinstance(value, dict)
            and value.get("schema_version") == 1
            and value.get("version") == str(version)
            and self._current_points(version, runtime)
            and self._doctor_unlocked() is True
        )

    def _journal_adapter_recovery(
        self,
        transaction: Path,
        journal: Mapping[str, object],
    ) -> tuple[dict[AssistantKind, Path], dict[AssistantKind, _AdapterBackup]]:
        raw_adapters = journal.get("adapters", {})
        if not isinstance(raw_adapters, dict):
            raise SetupError("stale setup adapter journal is invalid")
        changed: dict[AssistantKind, Path] = {}
        backups: dict[AssistantKind, _AdapterBackup] = {}
        for raw_kind, raw in raw_adapters.items():
            try:
                kind = AssistantKind(raw_kind)
            except (TypeError, ValueError) as error:
                raise SetupError("stale setup adapter journal is invalid") from error
            if not isinstance(raw, dict):
                raise SetupError("stale setup adapter journal is invalid")
            target_raw = raw.get("target")
            phase = raw.get("phase")
            backup_name = raw.get("backup")
            version_raw = raw.get("version")
            checksum = raw.get("checksum")
            fresh_target = raw.get("fresh_target")
            ownership = raw.get("ownership")
            recovery = raw.get("recovery")
            adapter = self._adapters.get(kind)
            if (
                adapter is None
                or not isinstance(target_raw, str)
                or Path(target_raw) != adapter.destination
                or phase not in {"prepared", "mutating", "mutated"}
                or type(fresh_target) is not bool
                or not isinstance(ownership, dict)
                or not all(type(value) is bool for value in ownership.values())
                or not isinstance(recovery, dict)
            ):
                raise SetupError("stale setup adapter journal is invalid")
            if phase == "prepared":
                continue
            version: Version | None = None
            if version_raw is not None:
                if not isinstance(version_raw, str):
                    raise SetupError("stale setup adapter journal is invalid")
                try:
                    version = Version(version_raw)
                except ValueError as error:
                    raise SetupError("stale setup adapter journal is invalid") from error
            if checksum is not None and not isinstance(checksum, str):
                raise SetupError("stale setup adapter journal is invalid")
            backup: Path | None = None
            if backup_name is not None:
                expected = f"previous-adapter-{kind.value}"
                if backup_name != expected or raw.get("backup_present") is not True:
                    raise SetupError("stale setup adapter journal is invalid")
                backup = transaction / expected
                _reject_linked_parents(backup)
            target = Path(target_raw)
            changed[kind] = target
            backups[kind] = _AdapterBackup(
                kind,
                target,
                backup,
                version,
                checksum,
                fresh_target,
                MappingProxyType(dict(ownership)),
                MappingProxyType(dict(recovery)),
            )
        return changed, backups

    def _require_owned_target(self, path: Path) -> None:
        root = self._data_root.absolute()
        target = Path(path).absolute()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise SetupError("stale setup transaction path is outside the data root") from error
        if target == root:
            raise SetupError("stale setup transaction path is invalid")
        _reject_linked_parents(target.parent)

    def _restore_component_backup(self, target: Path, backup: Path) -> None:
        if not (backup.exists() or backup.is_symlink()):
            return
        _reject_linked_parents(backup)
        if target.exists() or target.is_symlink():
            self._remove_owned(target)
        _prepare_private_parent(target.parent)
        _reject_linked_parents(backup.parent)
        os.replace(backup, target)
        _fsync_directory(target.parent)

    def _prepare(self, transaction: Path) -> None:
        staging = transaction.parent
        _prepare_private_parent(staging)
        _reject_linked_parents(staging)
        for entry in staging.iterdir():
            if entry != transaction:
                self._remove_owned(entry)
        _reject_linked_parents(staging)
        transaction.mkdir(mode=0o700)
        if os.name != "nt":
            os.chmod(transaction, 0o700)
        _fsync_directory(staging)

    @staticmethod
    def _activate(source: Path, target: Path, backup: Path) -> bool:
        _reject_linked_parents(source.parent)
        _prepare_private_parent(target.parent)
        _reject_linked_parents(backup.parent)
        replaced = target.exists() or target.is_symlink()
        if replaced:
            _reject_linked_parents(target.parent)
            os.replace(target, backup)
            _fsync_directory(target.parent)
            _fsync_directory(backup.parent)
        _reject_linked_parents(source.parent)
        _reject_linked_parents(target.parent)
        os.replace(source, target)
        _fsync_directory(target.parent)
        return replaced

    def _rollback(self, target: Path, backup: Path, activated: bool) -> None:
        _reject_linked_parents(target.parent)
        _reject_linked_parents(backup.parent)
        has_backup = backup.exists() or backup.is_symlink()
        if has_backup:
            _reject_linked_parents(backup)
            if target.exists() or target.is_symlink():
                self._remove_owned(target)
            _prepare_private_parent(target.parent)
            _reject_linked_parents(backup.parent)
            os.replace(backup, target)
            _fsync_directory(target.parent)
        elif activated and (target.exists() or target.is_symlink()):
            self._remove_owned(target)

    @staticmethod
    def _snapshot(path: Path) -> bytes | None:
        try:
            _reject_linked_parents(path)
            return path.read_bytes() if path.is_file() else None
        except SetupError:
            raise
        except OSError as error:
            raise SetupError("could not snapshot setup state") from error

    @staticmethod
    def _restore(path: Path, content: bytes | None) -> None:
        try:
            _prepare_private_parent(path.parent)
            _reject_linked_parents(path)
            if content is None:
                path.unlink(missing_ok=True)
                _fsync_directory(path.parent)
                return
            value = json.loads(content.decode("utf-8", errors="strict"))
            write_json_atomic(path, value)
        except SetupError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            raise SetupError("could not restore setup state") from error

    def _recover_files(
        self,
        *,
        runtime: Path,
        runtime_backup: Path,
        runtime_activated: bool,
        officecli: Path,
        office_backup: Path,
        office_activated: bool,
        state_snapshot: bytes | None,
        current_snapshot: bytes | None,
    ) -> bool:
        complete = True
        actions = (
            lambda: self._rollback(runtime, runtime_backup, runtime_activated),
            lambda: self._rollback(officecli, office_backup, office_activated),
            lambda: self._restore(self._data_root / "state.json", state_snapshot),
            lambda: self._restore(self._data_root / "current.json", current_snapshot),
        )
        for action in actions:
            try:
                action()
            except Exception:
                complete = False
        return complete

    def _cleanup(self, transaction: Path) -> None:
        try:
            _reject_linked_parents(transaction.parent)
            if transaction.exists() or transaction.is_symlink():
                self._remove_owned(transaction)
        except SetupError:
            pass

    def _remove_owned(self, path: Path) -> None:
        root = self._data_root.absolute()
        target = Path(path).absolute()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise SetupError("refused to remove a path outside the CSAF data root") from error
        if target == root:
            raise SetupError("refused to remove the CSAF data root")
        try:
            _reject_linked_parents(target.parent)
            try:
                details = target.lstat()
            except FileNotFoundError:
                return
            attributes = getattr(details, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat.S_ISLNK(details.st_mode) or attributes & reparse_flag:
                try:
                    target.unlink(missing_ok=True)
                except IsADirectoryError:
                    os.rmdir(target)
            elif stat.S_ISREG(details.st_mode):
                target.unlink(missing_ok=True)
            elif stat.S_ISDIR(details.st_mode):
                _reject_linked_parents(target)
                shutil.rmtree(target)
            _fsync_directory(target.parent)
        except SetupError:
            raise
        except OSError as error:
            raise SetupError("could not clean up CSAF setup files") from error
