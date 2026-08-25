"""Consent-first native setup lifecycle commands."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import sysconfig
import threading
import time
import unicodedata
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import click
import typer

from csaf.office.redaction import redact_officecli_message
from csaf.setup.assets import (
    AssetLimits,
    SetupError,
    _acquire_activation_lock,
    _prepare_private_parent,
    _reject_linked_parents,
    _release_activation_lock,
    extract_verified_archive,
    read_json,
    write_json_atomic,
)
from csaf.setup.manager import (
    ClaudeManagedAdapter,
    CodexManagedAdapter,
    GeminiManagedAdapter,
    InstallPlan,
    SetupManager,
    SetupResult,
    SetupStatus,
)
from csaf.setup.paths import (
    codex_skill_root,
    current_platform,
    default_data_root,
    detect_assistants,
    gemini_skill_root,
)
from csaf.setup.types import AssistantKind, InstallState, ReleaseManifest, Version

_RELEASE_ROOT = "https://github.com/karthiknambiar/csm-skills-framework/releases"
_LATEST_MANIFEST = f"{_RELEASE_ROOT}/latest/download/csaf-release-manifest.json"
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:[.]|$)", re.IGNORECASE
)
_MAX_MANIFEST_BYTES = 1024 * 1024
_CACHE_AGE = timedelta(hours=24)
_OFFICECLI_VERSION = Version("1.0.143")
# Shared Task 6 bootstrap layout. The bootstrap scripts must keep this private
# Python environment after removing only their verified wheel staging directory.
_PRIVATE_BIN_DIRECTORY = "bin"
_PRIVATE_PYTHON_DIRECTORY = "python"
_PRIVATE_UV_CACHE = Path("cache") / "uv"
_PRIVATE_UV_NAME = "uv.exe" if os.name == "nt" else "uv"
_RUNTIME_LAUNCHER_NAME = "csaf.exe" if os.name == "nt" else "csaf"
_RUNTIME_INSTALL_TIMEOUT = 120.0
_RUNTIME_BUNDLE_LIMITS = AssetLimits(
    max_archive_bytes=512 * 1024 * 1024,
    max_members=256,
    max_member_bytes=256 * 1024 * 1024,
    max_total_bytes=1024 * 1024 * 1024,
)
_RUNTIME_MEMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RUNTIME_CSAF_WHEEL = re.compile(r"^csaf-([0-9]+[.][0-9]+[.][0-9]+)-py3-none-any[.]whl$")
_RUNTIME_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*==[^\s]+ --hash=sha256:([0-9a-f]{64})$"
)
_CACHE_THREAD_LOCKS: dict[str, threading.Lock] = {}
_CACHE_THREAD_LOCKS_GUARD = threading.Lock()
_TERMINAL_STRING = re.compile(
    r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f]).*?(?:\x07|\x9c|\x1b\\|$)",
    re.DOTALL,
)
_TERMINAL_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_TERMINAL_ESCAPE = re.compile(r"\x1b[ -/]*[@-~]")

setup_app = typer.Typer(help="Install, diagnose, update, or remove native CSAF.")


class SetupManagerLike(Protocol):
    def plan_install(
        self,
        manifest: ReleaseManifest,
        *,
        requested_targets: Sequence[AssistantKind] | None,
    ) -> InstallPlan: ...

    def install(
        self,
        plan: InstallPlan,
        *,
        consent: Callable[[InstallPlan], bool],
        assume_yes: bool = False,
    ) -> SetupResult: ...

    def repair(
        self,
        plan: InstallPlan,
        *,
        consent: Callable[[InstallPlan], bool],
        assume_yes: bool = False,
    ) -> SetupResult: ...

    def check_update(self, manifest: ReleaseManifest) -> bool: ...

    def update(
        self,
        plan: InstallPlan,
        *,
        consent: Callable[[InstallPlan], bool],
        assume_yes: bool = False,
    ) -> SetupResult: ...

    def doctor(self) -> bool: ...

    def uninstall(
        self,
        *,
        consent: Callable[[], bool],
        include_officecli: bool = False,
        assume_yes: bool = False,
    ) -> SetupResult: ...


class ManifestResolverLike(Protocol):
    def resolve(self, source: str | None = None) -> ReleaseManifest: ...

    def resolve_version(self, version: Version, source: str | None = None) -> ReleaseManifest: ...

    def check(self, installed: Version | None, source: str | None = None) -> UpdateReport: ...


@dataclass(frozen=True, slots=True)
class UpdateReport:
    installed_version: Version | None
    available_version: Version | None
    available: bool
    cached: bool
    offline: bool
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "available_version": (
                str(self.available_version) if self.available_version is not None else None
            ),
            "cached": self.cached,
            "installed_version": (
                str(self.installed_version) if self.installed_version is not None else None
            ),
            "next_action": "csaf setup update" if self.available else "none",
            "offline": self.offline,
            "status": "ready",
        }


class _HttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: object,
        code: int,
        message: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urljoin(request.full_url, newurl)
        parts = urlsplit(target)
        if parts.scheme.casefold() != "https":
            raise SetupError("manifest redirect must remain HTTPS")
        if parts.username is not None or parts.password is not None:
            raise SetupError("release manifest URL must not contain credentials")
        return super().redirect_request(request, fp, code, message, headers, target)


class ReleaseResolver:
    """Resolve a strict release manifest from a local bootstrap file or HTTPS."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise SetupError("manifest timeout must be finite and positive")
        self._timeout = timeout

    def resolve(self, source: str | None = None) -> ReleaseManifest:
        location = _LATEST_MANIFEST if source is None else source
        try:
            local = self._local_path(location) if source is not None else None
            if local is not None:
                raw = self._read_local_manifest(local)
            else:
                parts = urlsplit(location)
                if parts.scheme.casefold() != "https":
                    raise SetupError("release manifest must use HTTPS")
                if parts.username is not None or parts.password is not None:
                    raise SetupError("release manifest URL must not contain credentials")
                if parts.query or parts.fragment:
                    raise SetupError("release manifest URL must not contain a query or fragment")
                request = urllib.request.Request(location, headers={"Accept-Encoding": "identity"})
                with urllib.request.build_opener(_HttpsRedirectHandler()).open(
                    request, timeout=self._timeout
                ) as response:
                    final = urlsplit(response.geturl())
                    if final.scheme.casefold() != "https":
                        raise SetupError("manifest redirect must remain HTTPS")
                    if final.username is not None or final.password is not None:
                        raise SetupError("release manifest URL must not contain credentials")
                    raw = response.read(_MAX_MANIFEST_BYTES + 1)
            if len(raw) > _MAX_MANIFEST_BYTES:
                raise SetupError("release manifest exceeds the size limit")
            return ReleaseManifest.model_validate_json(raw.decode("utf-8", errors="strict"))
        except SetupError:
            raise
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            http.client.HTTPException,
            ssl.SSLError,
        ) as error:
            raise SetupError("release manifest could not be resolved") from error

    def resolve_version(self, version: Version, source: str | None = None) -> ReleaseManifest:
        manifest = self.resolve(
            source or f"{_RELEASE_ROOT}/download/v{version}/csaf-release-manifest.json"
        )
        if manifest.version != version:
            raise SetupError("tagged release manifest version does not match the installed version")
        return manifest

    @staticmethod
    def _local_path(location: str) -> Path | None:
        if any(unicodedata.category(character) == "Cc" for character in location):
            raise SetupError("local release manifest path contains unsafe controls")
        normalized = location.replace("/", "\\")
        if normalized.startswith(("\\\\.\\", "\\\\?\\", "\\??\\")):
            raise SetupError("local release manifest path uses an unsafe device namespace")
        parts: tuple[str, ...] | None = None
        if _WINDOWS_ABSOLUTE.match(location) or location.startswith((r"\\", "//")):
            parts = PureWindowsPath(location).parts
        elif location.startswith("/"):
            parts = PurePosixPath(location).parts
        elif _WINDOWS_DRIVE.match(location):
            raise SetupError("local release manifest path must be absolute")
        elif urlsplit(location).scheme:
            return None
        else:
            raise SetupError("local release manifest path must be absolute")
        if any(part in {".", ".."} for part in parts):
            raise SetupError("local release manifest path contains traversal")
        windows_parts = PureWindowsPath(location).parts
        if any(_WINDOWS_RESERVED.match(part.rstrip(" .")) for part in windows_parts):
            raise SetupError("local release manifest path uses a reserved device name")
        return Path(location)

    @staticmethod
    def _read_local_manifest(path: Path) -> bytes:
        descriptor: int | None = None
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOINHERIT", 0)
        )
        if os.name != "nt":
            flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            _reject_linked_parents(path)
            descriptor = os.open(path, flags)
            details = os.fstat(descriptor)
            attributes = getattr(details, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if not stat.S_ISREG(details.st_mode) or attributes & reparse:
                raise SetupError("local release manifest must be a regular file")
            if details.st_size > _MAX_MANIFEST_BYTES:
                raise SetupError("release manifest exceeds the size limit")
            with os.fdopen(descriptor, "rb", closefd=True) as source_file:
                descriptor = None
                return source_file.read(_MAX_MANIFEST_BYTES + 1)
        except SetupError as error:
            if "symlink or reparse" in str(error):
                raise SetupError("local release manifest must be a regular file") from error
            raise
        except FileNotFoundError as error:
            raise SetupError("release manifest could not be resolved") from error
        except OSError as error:
            raise SetupError("local release manifest must be a regular file") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)


class UpdateCache:
    """Cache stable-version notifications without downloading or installing assets."""

    def __init__(
        self,
        data_root: Path,
        *,
        resolver: Callable[[str | None], ReleaseManifest] | None = None,
        clock: Callable[[], datetime] | None = None,
        lock_timeout: float = 2.0,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if not math.isfinite(lock_timeout) or lock_timeout <= 0:
            raise SetupError("update cache lock timeout must be finite and positive")
        self._path = Path(data_root) / "update-cache.json"
        self._lock_path = self._path.parent / ".update-cache.lock"
        self._resolve = resolver or ReleaseResolver().resolve
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock_timeout = lock_timeout
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or time.sleep
        cache_key = os.path.normcase(str(self._path.absolute()))
        with _CACHE_THREAD_LOCKS_GUARD:
            self._thread_lock = _CACHE_THREAD_LOCKS.setdefault(cache_key, threading.Lock())

    def resolve(self, source: str | None = None) -> ReleaseManifest:
        return self._resolve(source)

    def resolve_version(self, version: Version, source: str | None = None) -> ReleaseManifest:
        manifest = self._resolve(
            source or f"{_RELEASE_ROOT}/download/v{version}/csaf-release-manifest.json"
        )
        if manifest.version != version:
            raise SetupError("tagged release manifest version does not match the installed version")
        return manifest

    def check(self, installed: Version | None, source: str | None = None) -> UpdateReport:
        with self._thread_lock:
            return self._check_serialized(installed, source)

    def _check_serialized(
        self, installed: Version | None, source: str | None = None
    ) -> UpdateReport:
        now = self._utc_now()
        cached = self._read_cached(now, installed)
        if cached is not None:
            return cached
        descriptor: int | None = None
        deadline = self._monotonic() + self._lock_timeout
        try:
            _prepare_private_parent(self._path.parent)
            while descriptor is None:
                try:
                    descriptor = _acquire_activation_lock(self._lock_path)
                except SetupError:
                    cached = self._read_cached(now, installed)
                    if cached is not None:
                        return cached
                    if self._monotonic() >= deadline:
                        return self._offline(installed)
                    self._sleep(min(0.025, max(0.0, deadline - self._monotonic())))
            cached = self._read_cached(now, installed)
            if cached is not None:
                return cached
            manifest = self._resolve(source)
            report = UpdateReport(
                installed_version=installed,
                available_version=manifest.version,
                available=installed is None or installed < manifest.version,
                cached=False,
                offline=False,
            )
            try:
                write_json_atomic(
                    self._path,
                    {
                        "schema_version": 1,
                        "checked_at": now.isoformat(),
                        "available_version": str(manifest.version),
                    },
                )
            except (OSError, SetupError):
                pass
            return report
        except (OSError, SetupError):
            return self._offline(installed)
        finally:
            if descriptor is not None:
                try:
                    _release_activation_lock(descriptor)
                except OSError:
                    pass

    @staticmethod
    def _offline(installed: Version | None) -> UpdateReport:
        return UpdateReport(
            installed_version=installed,
            available_version=None,
            available=False,
            cached=False,
            offline=True,
            error="update check is unavailable",
        )

    def _read_cached(self, now: datetime, installed: Version | None) -> UpdateReport | None:
        try:
            value = read_json(self._path)
            if not isinstance(value, Mapping) or value.get("schema_version") != 1:
                return None
            checked_at = datetime.fromisoformat(value["checked_at"])
            available = Version(value["available_version"])
            if checked_at.tzinfo is None:
                return None
            age = now - checked_at.astimezone(UTC)
            if age < timedelta(0) or age >= _CACHE_AGE:
                return None
            return UpdateReport(
                installed_version=installed,
                available_version=available,
                available=installed is None or installed < available,
                cached=True,
                offline=False,
            )
        except (KeyError, OSError, SetupError, TypeError, ValueError):
            return None

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise SetupError("update clock must be timezone-aware")
        return value.astimezone(UTC)


def _regular_unlinked_file(path: Path) -> bool:
    if not path.is_absolute():
        return False
    try:
        _reject_linked_parents(path)
        details = path.lstat()
    except (OSError, SetupError):
        return False
    attributes = getattr(details, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISREG(details.st_mode) and not attributes & reparse


def _validated_runtime_bundle(
    bundle: Path, destination: Path, expected_version: Version
) -> tuple[Path, Path, Path]:
    bundle_root = destination.parent / ".runtime-bundle"
    try:
        try:
            extracted = extract_verified_archive(bundle, bundle_root, limits=_RUNTIME_BUNDLE_LIMITS)
        except SetupError as error:
            raise SetupError("runtime bundle is invalid") from error
        names = {path.relative_to(bundle_root).as_posix() for path in extracted}
        manifest_path = bundle_root / "runtime-bundle.json"
        raw_manifest = manifest_path.read_bytes()
        if len(raw_manifest) > 1024 * 1024:
            raise SetupError("runtime bundle manifest is too large")
        manifest = json.loads(raw_manifest.decode("utf-8"))
        if type(manifest) is not dict or set(manifest) != {
            "schema_version",
            "version",
            "platform",
            "files",
        }:
            raise SetupError("runtime bundle manifest is invalid")
        if manifest["schema_version"] != 1 or type(manifest["schema_version"]) is not int:
            raise SetupError("runtime bundle manifest is invalid")
        if type(manifest["version"]) is not str or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+", manifest["version"]
        ):
            raise SetupError("runtime bundle manifest is invalid")
        if manifest["version"] != str(expected_version):
            raise SetupError("runtime bundle version does not match the release manifest")
        if manifest["platform"] != current_platform().value:
            raise SetupError("runtime bundle platform does not match this system")
        files = manifest["files"]
        if type(files) is not dict or not files:
            raise SetupError("runtime bundle manifest is invalid")
        declared = set(files)
        if names != declared | {"runtime-bundle.json"}:
            raise SetupError("runtime bundle members do not match the manifest")
        runtime_wheels = sorted(name for name in declared if _RUNTIME_CSAF_WHEEL.fullmatch(name))
        expected_runtime_wheel = f"csaf-{expected_version}-py3-none-any.whl"
        if runtime_wheels != [expected_runtime_wheel] or "requirements.lock" not in declared:
            raise SetupError("runtime bundle is incomplete")
        wheel_names = declared - {expected_runtime_wheel, "requirements.lock"}
        if not wheel_names or any(
            not name.startswith("wheelhouse/")
            or not name.endswith(".whl")
            or not _RUNTIME_MEMBER.fullmatch(name.removeprefix("wheelhouse/"))
            for name in wheel_names
        ):
            raise SetupError("runtime bundle wheelhouse is invalid")
        for name, expected in files.items():
            if (
                type(name) is not str
                or type(expected) is not dict
                or set(expected)
                != {
                    "sha256",
                    "size",
                }
            ):
                raise SetupError("runtime bundle manifest is invalid")
            if (
                type(expected["sha256"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])
                or type(expected["size"]) is not int
                or expected["size"] <= 0
            ):
                raise SetupError("runtime bundle manifest is invalid")
            payload = bundle_root.joinpath(*name.split("/"))
            if not _regular_unlinked_file(payload):
                raise SetupError("runtime bundle member is unsafe")
            if payload.stat().st_size != expected["size"]:
                raise SetupError("runtime bundle member size does not match")
            if hashlib.sha256(payload.read_bytes()).hexdigest() != expected["sha256"]:
                raise SetupError("runtime bundle member checksum does not match")
        requirements = bundle_root / "requirements.lock"
        lock_text = requirements.read_text(encoding="utf-8")
        lines = lock_text.splitlines()
        if not lines or any(not line or line != line.strip() for line in lines):
            raise SetupError("runtime bundle requirements lock is invalid")
        runtime_hash = files[expected_runtime_wheel]["sha256"]
        if lines[0] != f"./{expected_runtime_wheel} --hash=sha256:{runtime_hash}":
            raise SetupError("runtime bundle requirements lock is invalid")
        dependency_hashes: list[str] = []
        for line in lines[1:]:
            match = _RUNTIME_REQUIREMENT.fullmatch(line)
            if match is None:
                raise SetupError("runtime bundle requirements lock is invalid")
            dependency_hashes.append(match.group(1))
        expected_hashes = sorted(files[name]["sha256"] for name in wheel_names)
        if sorted(dependency_hashes) != expected_hashes:
            raise SetupError("runtime bundle requirements lock is incomplete")
        return bundle_root, requirements, bundle_root / "wheelhouse"
    except SetupError:
        shutil.rmtree(bundle_root, ignore_errors=True)
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        shutil.rmtree(bundle_root, ignore_errors=True)
        raise SetupError("runtime bundle is invalid") from error


def _install_runtime(
    bundle: Path,
    destination: Path,
    *,
    expected_version: Version,
    uv_path: Path,
    launcher_path: Path,
    python_executable: Path,
    runner: Callable[..., object] = subprocess.run,
) -> Path:
    """Install one verified offline runtime bundle into the version transaction."""
    bundle = Path(bundle)
    destination = Path(destination)
    uv_path = Path(uv_path)
    launcher_path = Path(launcher_path)
    python_executable = Path(python_executable)
    if not all(
        _regular_unlinked_file(item) for item in (bundle, uv_path, launcher_path, python_executable)
    ):
        raise SetupError("private runtime bootstrap is unavailable; rerun the CSAF installer")
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise SetupError("private runtime destination is unsafe")
    try:
        _reject_linked_parents(destination.parent)
    except SetupError as error:
        raise SetupError("private runtime destination is unsafe") from error
    bundle_root, requirements, wheelhouse = _validated_runtime_bundle(
        bundle, destination, expected_version
    )
    site_packages = destination / "site-packages"
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR")
        if key in os.environ
    }
    data_root = uv_path.parent.parent
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "UV_CACHE_DIR": str(data_root / _PRIVATE_UV_CACHE),
            "UV_NO_CONFIG": "1",
            "UV_OFFLINE": "1",
            "UV_PYTHON_INSTALL_DIR": str(data_root / _PRIVATE_PYTHON_DIRECTORY),
            "UV_UNMANAGED_INSTALL": str(data_root / _PRIVATE_BIN_DIRECTORY),
        }
    )
    arguments = [
        str(uv_path),
        "pip",
        "install",
        "--python",
        str(python_executable),
        "--target",
        str(site_packages),
        "--offline",
        "--no-config",
        "--no-index",
        "--require-hashes",
        "--find-links",
        str(wheelhouse),
        "--requirement",
        str(requirements),
    ]
    try:
        result = runner(
            arguments,
            cwd=bundle_root,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_RUNTIME_INSTALL_TIMEOUT,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SetupError("private runtime installation failed; rerun csaf setup install") from error
    finally:
        shutil.rmtree(bundle_root, ignore_errors=True)
    if getattr(result, "returncode", 1) != 0:
        raise SetupError("private runtime installation failed; rerun csaf setup install")
    try:
        _reject_linked_parents(destination)
        installed = (destination, *destination.rglob("*"))
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        for item in installed:
            details = item.lstat()
            if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & reparse:
                raise SetupError("private runtime installation produced unsafe links")
        package = site_packages / "csaf" / "__init__.py"
        metadata = tuple(site_packages.glob("csaf-*.dist-info/METADATA"))
        if not package.is_file() or len(metadata) != 1:
            raise SetupError("private runtime installation is incomplete")
        target_launcher = destination / _RUNTIME_LAUNCHER_NAME
        if target_launcher.exists() or target_launcher.is_symlink():
            raise SetupError("private runtime installation produced an unsafe launcher")
        with launcher_path.open("rb") as source, target_launcher.open("xb") as target:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise SetupError(
                    "private runtime bootstrap is unavailable; rerun the CSAF installer"
                )
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if os.name != "nt":
            os.chmod(target_launcher, 0o700)
        return destination
    except SetupError:
        raise
    except OSError as error:
        raise SetupError("private runtime installation is incomplete") from error


def _runtime_installer(data_root: Path) -> Callable[[Path, Path, Version], Path]:
    uv_path = data_root / _PRIVATE_BIN_DIRECTORY / _PRIVATE_UV_NAME
    launcher_path = Path(sysconfig.get_path("scripts")) / _RUNTIME_LAUNCHER_NAME

    def install(wheel: Path, destination: Path, expected_version: Version) -> Path:
        return _install_runtime(
            wheel,
            destination,
            expected_version=expected_version,
            uv_path=uv_path,
            launcher_path=launcher_path,
            python_executable=Path(sys.executable),
        )

    return install


def _make_manager() -> SetupManager:
    data_root = default_data_root()
    adapters = {
        AssistantKind.CODEX: CodexManagedAdapter(codex_skill_root()),
        AssistantKind.CLAUDE: ClaudeManagedAdapter(data_root / "adapters" / "claude"),
        AssistantKind.GEMINI: GeminiManagedAdapter(gemini_skill_root()),
    }
    return SetupManager(
        data_root=data_root,
        platform=current_platform(),
        detected_assistants=detect_assistants(),
        adapter_installers=adapters,
        runtime_installer=_runtime_installer(data_root),
    )


def _make_resolver() -> UpdateCache:
    return UpdateCache(default_data_root())


def _safe_message(value: object) -> str:
    redacted = redact_officecli_message(str(value))
    return _display_text(redacted)[:512] or "native setup failed"


def _emit_json(value: Mapping[str, object]) -> None:
    typer.echo(json.dumps(dict(value), indent=2, sort_keys=True))


def _targets(
    codex_only: bool,
    claude_only: bool,
    gemini_only: bool,
) -> tuple[AssistantKind, ...] | None:
    overrides = {
        AssistantKind.CODEX: codex_only,
        AssistantKind.CLAUDE: claude_only,
        AssistantKind.GEMINI: gemini_only,
    }
    selected = tuple(kind for kind, enabled in overrides.items() if enabled)
    if len(selected) > 1:
        raise SetupError("choose only one assistant-only target override")
    return selected or None


def _display_text(value: object) -> str:
    """Return a single printable line without changing the operational value."""
    displayed = _TERMINAL_STRING.sub(" ", str(value))
    displayed = _TERMINAL_CSI.sub(" ", displayed)
    displayed = _TERMINAL_ESCAPE.sub(" ", displayed)
    displayed = "".join(
        character for character in displayed if not unicodedata.category(character).startswith("C")
    )
    return " ".join(displayed.split())


def _display_manifest_source(source: str | None) -> str:
    if source is None:
        return "latest tagged stable CSAF manifest"
    cleaned = _display_text(source)
    parts = urlsplit(cleaned)
    if parts.scheme.casefold() == "https":
        host = parts.hostname or "invalid HTTPS origin"
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        return _display_text(urlunsplit(("https", host, parts.path, "", "")))
    return _display_text(cleaned)


def _show_plan(plan: InstallPlan, *, action: str, source: str | None = None) -> None:
    typer.echo(f"CSAF version: {plan.manifest.version}")
    typer.echo(
        f"OfficeCLI version: {plan.manifest.officecli.version} "
        "(mandatory for QBR PowerPoint and Word generation)"
    )
    if plan.targets:
        typer.echo("Assistants: " + ", ".join(kind.value for kind in plan.targets))
    else:
        typer.echo("Assistants: none detected")
        typer.echo(
            "No native adapter will be added; run csaf setup repair after installing an assistant."
        )
    typer.echo(f"CSAF runtime destination: {_display_text(plan.runtime_path)}")
    typer.echo(f"OfficeCLI destination: {_display_text(plan.officecli_path)}")
    for kind in plan.targets:
        if kind is AssistantKind.CLAUDE:
            typer.echo(
                f"Claude CSAF receipt destination: {_display_text(plan.adapter_destinations[kind])}"
            )
            typer.echo("Claude Code adapter: user-scoped marketplace/plugin managed by Claude CLI")
        else:
            typer.echo(
                f"{kind.value.title()} adapter destination: "
                f"{_display_text(plan.adapter_destinations[kind])}"
            )
    typer.echo(f"Release source: {_display_manifest_source(source)}")
    typer.echo("Network: HTTPS downloads of verified CSAF, OfficeCLI, and adapter assets")
    typer.echo(f"Requested action: {action}")


def _confirm(prompt: str) -> bool:
    try:
        return typer.confirm(prompt, default=False)
    except (click.Abort, EOFError):
        return False


def _finish(result: SetupResult) -> None:
    typer.echo(f"Status: {result.status.value}")
    if result.active_version is not None:
        typer.echo(f"Active CSAF version: {result.active_version}")
    if result.error:
        typer.echo(f"Error: {_safe_message(result.error)}", err=True)
    if result.status is not SetupStatus.READY:
        typer.echo("Next action: csaf setup doctor")
        raise typer.Exit(code=2)


def _manager_data_root(manager: SetupManagerLike) -> Path:
    root = getattr(manager, "root", None) or getattr(manager, "_data_root", None)
    if root is None:
        raise SetupError("native setup data root is unavailable")
    return Path(root)


def _recorded_state(manager: SetupManagerLike) -> InstallState | None:
    state_path = _manager_data_root(manager) / "state.json"
    if not state_path.is_file():
        return None
    try:
        return InstallState.model_validate(read_json(state_path))
    except Exception as error:
        raise SetupError("installed CSAF state is invalid") from error


def _installed_state(manager: SetupManagerLike) -> InstallState | None:
    state = _recorded_state(manager)
    if state is None or state.active_version is None:
        return None
    current_path = _manager_data_root(manager) / "current.json"
    if not current_path.is_file():
        return None
    try:
        current = read_json(current_path)
    except Exception as error:
        raise SetupError("installed CSAF state is invalid") from error
    runtime = state.runtime_paths.get(state.active_version)
    if (
        runtime is None
        or not isinstance(current, dict)
        or set(current) != {"schema_version", "active_version", "runtime_path"}
        or type(current.get("schema_version")) is not int
        or current.get("schema_version") != 1
        or current.get("active_version") != str(state.active_version)
        or current.get("runtime_path") != str(runtime)
    ):
        raise SetupError("installed CSAF state does not match the active runtime")
    return state


def _installed_version(manager: SetupManagerLike) -> Version | None:
    state = _installed_state(manager)
    return state.active_version if state is not None else None


def _check_update(manager: SetupManagerLike, resolver: ManifestResolverLike) -> UpdateReport:
    return resolver.check(_installed_version(manager))


def _show_uninstall_plan(manager: SetupManagerLike, *, include_officecli: bool) -> None:
    root = _manager_data_root(manager)
    state = _recorded_state(manager)
    version = state.active_version if state is not None else None
    recorded_versions = tuple(sorted(state.installed_versions)) if state is not None else ()
    displayed_version = version or (recorded_versions[-1] if recorded_versions else None)
    office_version = state.officecli_version if state is not None else None
    assistants = (
        tuple(kind for kind in AssistantKind if kind in state.adapter_targets)
        if state is not None
        else ()
    )
    if version is not None:
        typer.echo(f"CSAF version: {version}")
    elif displayed_version is not None:
        typer.echo(f"CSAF version: {displayed_version} (not active)")
    else:
        typer.echo("CSAF version: not installed")
    typer.echo(
        f"OfficeCLI version: {office_version or 'not recorded'} (mandatory for QBR documents)"
    )
    ownership = bool(state and state.officecli_installed_by_csaf)
    typer.echo(
        "OfficeCLI ownership: " + ("installed by CSAF" if ownership else "not installed by CSAF")
    )
    typer.echo(
        "Assistants: "
        + (", ".join(kind.value for kind in assistants) if assistants else "none recorded")
    )
    typer.echo(f"CSAF data destination: {_display_text(root)}")
    if state is not None:
        for installed_version in sorted(state.runtime_paths):
            typer.echo(
                "CSAF runtime destination: " + _display_text(state.runtime_paths[installed_version])
            )
        if state.officecli_path is not None:
            typer.echo(f"OfficeCLI destination: {_display_text(state.officecli_path)}")
        for kind in assistants:
            typer.echo(
                f"{kind.value.title()} adapter destination: "
                f"{_display_text(state.adapter_targets[kind])}"
            )
    typer.echo("Network: none")
    typer.echo("CSAF runtime and installed native adapters will be removed.")
    if include_officecli:
        typer.echo("OfficeCLI will be removed only if CSAF installed it.")
    else:
        typer.echo("OfficeCLI will be retained.")


def _fail(error: object) -> None:
    typer.echo(f"Error: {_safe_message(error)}", err=True)
    raise typer.Exit(code=2)


@setup_app.command("install")
def install(
    assume_yes: Annotated[bool, typer.Option("--yes", help="Approve the displayed plan.")] = False,
    codex_only: Annotated[bool, typer.Option("--codex-only")] = False,
    claude_only: Annotated[bool, typer.Option("--claude-only")] = False,
    gemini_only: Annotated[bool, typer.Option("--gemini-only")] = False,
    manifest_source: Annotated[
        str | None, typer.Option("--manifest", help="Tagged manifest HTTPS URL or local file.")
    ] = None,
) -> None:
    """Install CSAF, mandatory OfficeCLI, and selected native adapters."""
    try:
        requested = _targets(codex_only, claude_only, gemini_only)
        manager = _make_manager()
        resolver = _make_resolver()
        plan = manager.plan_install(resolver.resolve(manifest_source), requested_targets=requested)
        _show_plan(plan, action="install", source=manifest_source)
        if not assume_yes and not _confirm("Proceed with installation?"):
            raise typer.Exit(code=2)
        _finish(manager.install(plan, consent=lambda _plan: True, assume_yes=True))
    except typer.Exit:
        raise
    except (OSError, SetupError, ValueError) as error:
        _fail(error)


def _planned_operation(action: str, assume_yes: bool) -> None:
    try:
        manager = _make_manager()
        resolver = _make_resolver()
        if action == "repair":
            state = _installed_state(manager)
            if state is None or state.active_version is None:
                raise SetupError("no active CSAF installation; run csaf setup install")
            manifest = resolver.resolve_version(state.active_version)
            if manifest.version != state.active_version:
                raise SetupError("repair manifest does not match the active CSAF version")
            targets = tuple(kind for kind in AssistantKind if kind in state.adapter_targets)
            source = f"tagged stable CSAF v{state.active_version} manifest"
        else:
            manifest = resolver.resolve()
            if not manager.check_update(manifest):
                typer.echo("Status: ready")
                typer.echo("No stable CSAF update is available.")
                return
            targets = None
            source = None
        plan = manager.plan_install(manifest, requested_targets=targets)
        _show_plan(plan, action=action, source=source)
        if not assume_yes and not _confirm(f"Proceed with {action}?"):
            raise typer.Exit(code=2)
        operation = manager.repair if action == "repair" else manager.update
        _finish(operation(plan, consent=lambda _plan: True, assume_yes=True))
    except typer.Exit:
        raise
    except (OSError, SetupError, ValueError) as error:
        _fail(error)


@setup_app.command("repair")
def repair(
    assume_yes: Annotated[bool, typer.Option("--yes", help="Approve the displayed plan.")] = False,
) -> None:
    """Repair only missing or damaged CSAF components."""
    _planned_operation("repair", assume_yes)


@setup_app.command("update")
def update(
    assume_yes: Annotated[bool, typer.Option("--yes", help="Approve the displayed plan.")] = False,
) -> None:
    """Apply an available tagged stable release after consent."""
    _planned_operation("update", assume_yes)


@setup_app.command("doctor")
def doctor(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Diagnose the runtime, OfficeCLI, adapters, and permissions."""
    try:
        ready = _make_manager().doctor()
        report = {
            "next_action": "none" if ready else "csaf setup repair",
            "status": "ready" if ready else "failed",
        }
        if json_output:
            _emit_json(report)
        else:
            typer.echo(f"Status: {report['status']}")
            typer.echo(f"Next action: {report['next_action']}")
        if not ready:
            raise typer.Exit(code=2)
    except typer.Exit:
        raise
    except (OSError, SetupError, ValueError) as error:
        _fail(error)


@setup_app.command("check-update")
def check_update(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check for a stable release and notify without installing it."""
    try:
        report = _check_update(_make_manager(), _make_resolver())
        if json_output:
            _emit_json(report.as_dict())
            return
        typer.echo("Status: ready")
        if report.installed_version is not None:
            typer.echo(f"Installed version: {report.installed_version}")
        if report.available:
            typer.echo(f"Available version: {report.available_version}")
            typer.echo("Update available. Run: csaf setup update")
        elif report.offline:
            typer.echo("Update check unavailable; the installed local runtime remains usable.")
            typer.echo("Retry later with: csaf setup check-update")
        else:
            typer.echo("No stable CSAF update is available.")
    except (OSError, SetupError, ValueError) as error:
        _fail(error)


@setup_app.command("uninstall")
def uninstall(
    assume_yes: Annotated[bool, typer.Option("--yes", help="Approve removal.")] = False,
    include_officecli: Annotated[bool, typer.Option("--include-officecli")] = False,
) -> None:
    """Remove CSAF and native adapters, optionally including owned OfficeCLI."""
    try:
        manager = _make_manager()
        _show_uninstall_plan(manager, include_officecli=include_officecli)
        if not assume_yes and not _confirm("Proceed with uninstall?"):
            raise typer.Exit(code=2)
        _finish(
            manager.uninstall(
                consent=lambda: True,
                include_officecli=include_officecli,
                assume_yes=True,
            )
        )
    except typer.Exit:
        raise
    except (OSError, SetupError, ValueError) as error:
        _fail(error)


def main() -> None:
    """Run the native setup command group directly."""
    setup_app()


if __name__ == "__main__":  # pragma: no cover - exercised by installer smoke tests
    main()
