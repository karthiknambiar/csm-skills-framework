"""Build deterministic offline native CSAF release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

PLATFORM_IDS = (
    "linux-arm64",
    "linux-x64",
    "macos-arm64",
    "macos-x64",
    "windows-arm64",
    "windows-x64",
)
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_RELEASE_BASE = "https://github.com/karthiknambiar/csm-skills-framework/releases/download"
_TEXT_SUFFIXES = {".json", ".md", ".ps1", ".sh", ".txt", ".yaml", ".yml"}
MAX_RELEASE_ARCHIVE_BYTES = 536_870_912
MAX_RELEASE_ARCHIVE_MEMBERS = 10_000
MAX_RELEASE_MEMBER_BYTES = 134_217_728
MAX_RELEASE_TOTAL_BYTES = 536_870_912
MAX_RELEASE_COMPRESSION_RATIO = 200
_HASH_CHUNK_BYTES = 1_048_576
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_WINDOWS_INVALID_CHARS = frozenset('<>":|?*')


class ReleaseBuildError(RuntimeError):
    """Release inputs cannot produce a verified release."""


class ReleaseDurabilityError(ReleaseBuildError):
    """Publication succeeded but parent-directory durability is uncertain."""


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path, *, max_bytes: int = MAX_RELEASE_ARCHIVE_BYTES) -> str:
    try:
        info = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_size < 0
            or info.st_size > max_bytes
        ):
            raise ReleaseBuildError(f"release artifact size limit exceeded: {path.name}")
        digest = hashlib.sha256()
        observed = 0
        with path.open("rb") as source:
            while chunk := source.read(_HASH_CHUNK_BYTES):
                observed += len(chunk)
                if observed > max_bytes:
                    raise ReleaseBuildError(f"release artifact size limit exceeded: {path.name}")
                digest.update(chunk)
        if observed != info.st_size:
            raise ReleaseBuildError(f"release artifact size changed: {path.name}")
        return digest.hexdigest()
    except OSError as exc:
        raise ReleaseBuildError(f"release artifact is invalid: {path.name}") from exc


def _asset(path: Path, version: str) -> dict[str, object]:
    return {
        "url": f"{_RELEASE_BASE}/v{version}/{path.name}",
        "sha256": _hash_file(path),
        "size": path.stat().st_size,
    }


def _validated_archive_names(names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    validated: list[str] = []
    normalized: set[str] = set()
    for name in names:
        raw_parts = name.split("/")
        if (
            not name
            or "\\" in name
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in raw_parts)
            or any(
                unicodedata.category(character).startswith("C")
                or character in _WINDOWS_INVALID_CHARS
                for character in name
            )
            or any(part.endswith((".", " ")) for part in raw_parts)
            or any(part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES for part in raw_parts)
        ):
            raise ReleaseBuildError(f"unsafe archive member: {name!r}")
        key = unicodedata.normalize("NFC", "/".join(raw_parts)).casefold()
        if key in normalized:
            raise ReleaseBuildError(f"unsafe archive member collision: {name!r}")
        normalized.add(key)
        validated.append(name)
    return tuple(validated)


def _validated_archive_infos(archive: zipfile.ZipFile, path: Path) -> tuple[zipfile.ZipInfo, ...]:
    infos = tuple(archive.infolist())
    if len(infos) > MAX_RELEASE_ARCHIVE_MEMBERS:
        raise ReleaseBuildError(f"archive member limit exceeded: {path.name}")
    _validated_archive_names(tuple(info.filename for info in infos))
    total = 0
    for info in infos:
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if (
            info.is_dir()
            or info.flag_bits & 0x1
            or file_type not in {0, stat.S_IFREG}
            or stat.S_ISLNK(mode)
        ):
            raise ReleaseBuildError(f"unsafe archive member: {info.filename!r}")
        if info.file_size < 0 or info.file_size > MAX_RELEASE_MEMBER_BYTES:
            raise ReleaseBuildError(f"archive member size limit exceeded: {path.name}")
        if info.compress_size < 0 or (
            info.file_size > 0
            and info.file_size > max(1, info.compress_size) * MAX_RELEASE_COMPRESSION_RATIO
        ):
            raise ReleaseBuildError(f"archive compression ratio exceeded: {path.name}")
        total += info.file_size
        if total > MAX_RELEASE_TOTAL_BYTES:
            raise ReleaseBuildError(f"archive expansion limit exceeded: {path.name}")
    return infos


def _read_archive_info(archive: zipfile.ZipFile, info: zipfile.ZipInfo, path: Path) -> bytes:
    try:
        with archive.open(info) as source:
            data = source.read(info.file_size + 1)
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        raise ReleaseBuildError(f"archive member is invalid: {path.name}") from exc
    if len(data) != info.file_size:
        raise ReleaseBuildError(f"archive member size changed: {path.name}")
    return data


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(f"invalid JSON input: {path}") from exc
    if not isinstance(value, dict):
        raise ReleaseBuildError(f"JSON input must be an object: {path}")
    return value


def _project(root: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    try:
        value = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        csaf = value["tool"]["csaf"]
        return value["project"]["version"], csaf["native-runtime"], csaf["native-release"]
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ReleaseBuildError("pyproject native release configuration is invalid") from exc


def _wheel_members(archive: zipfile.ZipFile, wheel: Path) -> tuple[str, ...]:
    return tuple(info.filename for info in _validated_archive_infos(archive, wheel))


def _wheel_metadata(path: Path) -> tuple[str, str, tuple[str, ...]]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = _validated_archive_infos(archive, path)
            info_by_name = {info.filename: info for info in infos}
            metadata_names = [name for name in info_by_name if name.endswith(".dist-info/METADATA")]
            wheel_names = [name for name in info_by_name if name.endswith(".dist-info/WHEEL")]
            if len(metadata_names) != 1 or len(wheel_names) != 1:
                raise ReleaseBuildError(f"wheel has invalid metadata: {path.name}")
            message = BytesParser().parsebytes(
                _read_archive_info(archive, info_by_name[metadata_names[0]], path)
            )
            wheel_message = BytesParser().parsebytes(
                _read_archive_info(archive, info_by_name[wheel_names[0]], path)
            )
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ReleaseBuildError(f"wheel is invalid: {path.name}") from exc
    names = message.get_all("Name", ())
    versions = message.get_all("Version", ())
    tags = tuple(wheel_message.get_all("Tag", ()))
    if len(names) != 1 or len(versions) != 1 or not tags:
        raise ReleaseBuildError(f"wheel metadata is incomplete: {path.name}")
    name, version = names[0], versions[0]
    metadata_directory = PurePosixPath(metadata_names[0]).parent.name
    try:
        directory_name, directory_version = metadata_directory.removesuffix(".dist-info").rsplit(
            "-", 1
        )
    except ValueError as exc:
        raise ReleaseBuildError(f"wheel metadata directory is invalid: {path.name}") from exc
    if _normal(directory_name) != _normal(name) or directory_version != version.replace("-", "_"):
        raise ReleaseBuildError(f"wheel metadata directory does not match identity: {path.name}")
    return name, version, tags


def _validate_csaf_wheel(path: Path, version: str) -> None:
    expected = f"csaf-{version}-py3-none-any.whl"
    if path.is_symlink() or not path.is_file() or path.name != expected:
        raise ReleaseBuildError("CSAF wheel filename does not match its exact release contract")
    name, actual_version, tags = _wheel_metadata(path)
    if name.casefold() != "csaf" or actual_version != version or tags != ("py3-none-any",):
        raise ReleaseBuildError("CSAF wheel metadata or embedded tag is invalid")


def _normal(value: str) -> str:
    return re.sub(r"[-_.]+", "_", value).casefold()


def _requirement(value: object) -> tuple[str, str, str]:
    if not isinstance(value, str) or value.count("==") != 1:
        raise ReleaseBuildError("native dependencies must be exact name==version pins")
    name, version = value.split("==")
    if not name or not version:
        raise ReleaseBuildError("native dependency pin is invalid")
    return name, version, value


def _compatible(path: Path, name: str, version: str, platform_tag: str) -> bool:
    prefix = f"{_normal(name)}-{version}-"
    if not path.name.casefold().startswith(prefix.casefold()) or not path.name.endswith(".whl"):
        return False
    tag = path.name[len(prefix) : -4]
    return tag in {"py3-none-any", "py2.py3-none-any"} or (
        tag.startswith("cp312-cp312-") and platform_tag in tag.rsplit("-", 1)[-1].split(".")
    )


def _validate_wheel(path: Path, name: str, version: str, platform_tag: str) -> None:
    if (
        path.is_symlink()
        or not path.is_file()
        or not _compatible(path, name, version, platform_tag)
    ):
        raise ReleaseBuildError(
            f"wheel is incompatible with CPython 3.12/{platform_tag}: {path.name}"
        )
    actual_name, actual_version, tags = _wheel_metadata(path)
    if _normal(actual_name) != _normal(name) or actual_version != version:
        raise ReleaseBuildError(f"wheel metadata does not match exact pin: {path.name}")
    if not any(
        tag in {"py3-none-any", "py2.py3-none-any"}
        or (tag.startswith("cp312-cp312-") and tag.endswith(platform_tag))
        for tag in tags
    ):
        raise ReleaseBuildError(f"wheel metadata tag is incompatible: {path.name}")


def _download(
    directory: Path,
    requirement: str,
    tag: str,
    native: dict[str, Any],
    *,
    expected_name: str | None = None,
    expected_hash: str | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys._base_executable,
            "-m",
            "pip",
            "download",
            requirement,
            "--dest",
            str(directory),
            "--only-binary=:all:",
            "--no-deps",
            "--python-version",
            "312",
            "--implementation",
            native["implementation"],
            "--abi",
            native["abi"],
            "--platform",
            tag,
            "--index-url",
            "https://pypi.org/simple",
            "--disable-pip-version-check",
            "--no-input",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    if result.returncode:
        raise ReleaseBuildError(f"authoritative PyPI wheel acquisition failed for {requirement}")
    wheels = list(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise ReleaseBuildError(f"PyPI returned an ambiguous wheel set for {requirement}")
    wheel = wheels[0]
    if expected_name is not None and (
        wheel.name != expected_name
        or expected_hash is None
        or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        or _hash(wheel.read_bytes()) != expected_hash
    ):
        raise ReleaseBuildError(f"authoritative PyPI wheel identity mismatch for {requirement}")
    return wheel


def _pinned_wheels(release: dict[str, Any], platform: str) -> dict[str, str]:
    try:
        wheels = release["wheels"]
        common = wheels["common"]
        platforms = wheels["platform"]
        specific = platforms[platform]
    except (KeyError, TypeError) as exc:
        raise ReleaseBuildError("native release wheel pins are incomplete") from exc
    if (
        not isinstance(common, dict)
        or not isinstance(specific, dict)
        or set(platforms) != set(PLATFORM_IDS)
        or set(common) & set(specific)
    ):
        raise ReleaseBuildError("native release wheel pin sets are invalid")
    pins = {**common, **specific}
    if not pins or any(
        not isinstance(name, str)
        or Path(name).name != name
        or not name.endswith(".whl")
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in pins.items()
    ):
        raise ReleaseBuildError("native release wheel pins are invalid")
    return pins


def _wheelhouse(
    platform: str,
    cache: Path | None,
    dependencies: list[tuple[str, str, str]],
    native: dict[str, Any],
    release: dict[str, Any],
    temporary: Path,
) -> list[tuple[str, bytes, str]]:
    tag = native["platform-tags"][platform]
    pins = _pinned_wheels(release, platform)
    if len(pins) != len(dependencies):
        raise ReleaseBuildError(f"native release wheel closure is incomplete for {platform}")
    expected_by_pin: dict[str, tuple[str, str]] = {}
    for name, version, pin in dependencies:
        matches = [filename for filename in pins if _compatible(Path(filename), name, version, tag)]
        if len(matches) != 1:
            raise ReleaseBuildError(f"native release wheel pins do not exactly match {pin}")
        filename = matches[0]
        expected_by_pin[pin] = (filename, pins[filename])
    if len({item[0] for item in expected_by_pin.values()}) != len(dependencies):
        raise ReleaseBuildError(f"native release wheel closure is ambiguous for {platform}")

    source = cache / platform if cache else None
    if source is not None and (source.is_symlink() or not source.is_dir()):
        raise ReleaseBuildError(f"wheelhouse is missing for {platform}")
    candidates = sorted(source.glob("*.whl")) if source else []
    if source and {path.name for path in candidates} != set(pins):
        raise ReleaseBuildError(f"wheelhouse does not exactly match immutable pins for {platform}")

    selected: list[tuple[str, bytes, str]] = []
    for name, version, pin in dependencies:
        expected_name, expected_hash = expected_by_pin[pin]
        wheel = (
            source / expected_name
            if source
            else _download(
                temporary / platform / _normal(name),
                pin,
                tag,
                native,
                expected_name=expected_name,
                expected_hash=expected_hash,
            )
        )
        if (
            wheel.is_symlink()
            or not wheel.is_file()
            or wheel.name != expected_name
            or _hash(wheel.read_bytes()) != expected_hash
        ):
            raise ReleaseBuildError(f"wheel hash or identity mismatch for {pin} on {platform}")
        _validate_wheel(wheel, name, version, tag)
        selected.append((wheel.name, wheel.read_bytes(), pin))
    return selected


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.flag_bits |= 0x800
    return info


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    names = _validated_archive_names(tuple(sorted(members)))
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(_zip_info(name), members[name])


def _file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ReleaseBuildError(f"release input is not a regular file: {path}")
    data = path.read_bytes()
    if path.suffix.casefold() in _TEXT_SUFFIXES:
        try:
            data = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode()
        except UnicodeDecodeError as exc:
            raise ReleaseBuildError(f"release text is not UTF-8: {path}") from exc
    return data


def _tree(root: Path, prefix: str) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise ReleaseBuildError(f"release input directory is missing or linked: {root}")
    members: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ReleaseBuildError(f"release input contains a symlink: {path}")
        if path.is_file():
            members[f"{prefix}/{path.relative_to(root).as_posix()}"] = _file(path)
    if not members:
        raise ReleaseBuildError(f"release input directory is empty: {root}")
    return members


def _clean(root: Path) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if result.returncode or result.stdout.strip():
        raise ReleaseBuildError("repository is dirty")


def _contract(
    root: Path, wheel: Path, requested: str | None
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    project_version, native, release = _project(root)
    wheel_name, wheel_version, _ = _wheel_metadata(wheel)
    plugin_version = _json(root / "plugins/csaf/.claude-plugin/plugin.json").get("version")
    try:
        marketplace_version = _json(root / ".claude-plugin/marketplace.json")["plugins"][0][
            "version"
        ]
    except (KeyError, IndexError, TypeError) as exc:
        raise ReleaseBuildError("marketplace version is invalid") from exc
    version = requested or wheel_version
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ReleaseBuildError("stable release version is invalid")
    if wheel_name.casefold() != "csaf" or any(
        item != version
        for item in (project_version, wheel_version, plugin_version, marketplace_version)
    ):
        raise ReleaseBuildError("release version agreement failed")
    _validate_csaf_wheel(wheel, version)
    return version, native, release


def _assemble_release(
    *,
    repo_root: Path,
    output_root: Path,
    wheel: Path,
    wheelhouse_root: Path | None = None,
    requested_version: str | None = None,
    require_clean: bool = True,
) -> Path:
    """Assemble one deterministic native release directory."""
    root, wheel = repo_root.resolve(), wheel.resolve()
    if require_clean:
        _clean(root)
    version, native, release = _contract(root, wheel, requested_version)
    runtime_wheel_name = f"csaf-{version}-py3-none-any.whl"
    if wheel.name != runtime_wheel_name:
        raise ReleaseBuildError("CSAF runtime wheel filename does not match its exact version")
    try:
        dependencies = [_requirement(item) for item in native["dependencies"]]
        tags = native["platform-tags"]
    except (KeyError, TypeError) as exc:
        raise ReleaseBuildError("native runtime configuration is incomplete") from exc
    if (
        not dependencies
        or set(tags) != set(PLATFORM_IDS)
        or native.get("python-version") != "3.12"
        or native.get("implementation") != "cp"
        or native.get("abi") != "cp312"
    ):
        raise ReleaseBuildError("native runtime configuration is incompatible")
    officecli = _json(root / "installer/dependencies.json").get("officecli")
    if not isinstance(officecli, dict):
        raise ReleaseBuildError("OfficeCLI dependency metadata is missing")
    destination = output_root.resolve() / version
    if destination.exists():
        raise ReleaseBuildError(f"release destination already exists: {destination}")
    destination.mkdir(parents=True)
    skill = _tree(root / "plugins/csaf/skills/csaf", "csaf")
    codex_name, claude_name = f"csaf-codex-skill-{version}.zip", f"csaf-claude-plugin-{version}.zip"
    _write_zip(destination / codex_name, skill)
    plugin = dict(skill)
    plugin[".claude-plugin/plugin.json"] = _file(root / "plugins/csaf/.claude-plugin/plugin.json")
    _write_zip(destination / claude_name, plugin)
    runtime_assets: dict[str, object] = {}
    runtime_wheel = wheel.read_bytes()
    with tempfile.TemporaryDirectory(prefix="csaf-native-wheels-") as temp:
        for platform in PLATFORM_IDS:
            wheels = _wheelhouse(
                platform,
                wheelhouse_root.resolve() if wheelhouse_root else None,
                dependencies,
                native,
                release,
                Path(temp),
            )
            lock = [f"./{runtime_wheel_name} --hash=sha256:{_hash(runtime_wheel)}"] + [
                f"{pin} --hash=sha256:{_hash(data)}" for _, data, pin in wheels
            ]
            members = {
                runtime_wheel_name: runtime_wheel,
                "requirements.lock": ("\n".join(lock) + "\n").encode(),
                **{f"wheelhouse/{name}": data for name, data, _ in wheels},
            }
            metadata = {
                "schema_version": 1,
                "version": version,
                "platform": platform,
                "files": {
                    name: {"sha256": _hash(data), "size": len(data)}
                    for name, data in sorted(members.items())
                },
            }
            members["runtime-bundle.json"] = _canonical_json(metadata)
            path = destination / f"csaf-runtime-{platform}-{version}.zip"
            _write_zip(path, members)
            runtime_assets[platform] = _asset(path, version)
    for name in ("install.ps1", "install.sh"):
        (destination / name).write_bytes(_file(root / "installer" / name))
    manifest = {
        "schema_version": 1,
        "version": version,
        "runtime": runtime_assets,
        "codex_skill": _asset(destination / codex_name, version),
        "claude_plugin": _asset(destination / claude_name, version),
        "officecli": officecli,
    }
    (destination / "csaf-release-manifest.json").write_bytes(_canonical_json(manifest))
    files = sorted(path for path in destination.iterdir() if path.is_file())
    (destination / "SHA256SUMS").write_text(
        "".join(f"{_hash(path.read_bytes())}  {path.name}\n" for path in files),
        encoding="utf-8",
        newline="\n",
    )
    return destination


def _archive_payloads(path: Path) -> dict[str, bytes]:
    try:
        info = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_size < 0
            or info.st_size > MAX_RELEASE_ARCHIVE_BYTES
        ):
            raise ReleaseBuildError(f"archive size limit exceeded: {path.name}")
        with zipfile.ZipFile(path) as archive:
            infos = _validated_archive_infos(archive, path)
            payloads: dict[str, bytes] = {}
            for member_info in infos:
                payloads[member_info.filename] = _read_archive_info(archive, member_info, path)
            return payloads
    except ReleaseBuildError:
        raise
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile, KeyError) as exc:
        raise ReleaseBuildError(f"release archive is invalid: {path.name}") from exc


def _verify_outer_sums(directory: Path, expected_names: set[str]) -> None:
    sums_path = directory / "SHA256SUMS"
    try:
        lines = sums_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseBuildError("release SHA256SUMS is invalid") from exc
    sums: dict[str, str] = {}
    for line in lines:
        if not re.fullmatch(r"[0-9a-f]{64}  [A-Za-z0-9][A-Za-z0-9_.-]*", line):
            raise ReleaseBuildError("release SHA256SUMS is invalid")
        digest, name = line.split("  ", 1)
        if name in sums:
            raise ReleaseBuildError("release SHA256SUMS contains duplicates")
        sums[name] = digest
    actual = {path.name for path in directory.iterdir() if path.is_file() and path != sums_path}
    if actual != expected_names or set(sums) != expected_names:
        raise ReleaseBuildError("release artifact set does not match SHA256SUMS")
    for name, digest in sums.items():
        if _hash_file(directory / name) != digest:
            raise ReleaseBuildError(f"release outer hash mismatch: {name}")


def _verify_runtime_archive(
    path: Path,
    *,
    platform: str,
    version: str,
    native: dict[str, Any],
    release: dict[str, Any],
) -> None:
    payloads = _archive_payloads(path)
    try:
        metadata_bytes = payloads.pop("runtime-bundle.json")
        metadata = json.loads(metadata_bytes)
    except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(f"runtime metadata is invalid for {platform}") from exc
    if metadata_bytes != _canonical_json(metadata) or set(metadata) != {
        "schema_version",
        "version",
        "platform",
        "files",
    }:
        raise ReleaseBuildError(f"runtime metadata is not canonical for {platform}")
    if (
        metadata["schema_version"] != 1
        or metadata["version"] != version
        or metadata["platform"] != platform
        or not isinstance(metadata["files"], dict)
        or set(metadata["files"]) != set(payloads)
    ):
        raise ReleaseBuildError(f"runtime metadata contract mismatch for {platform}")
    for name, data in payloads.items():
        if metadata["files"][name] != {"sha256": _hash(data), "size": len(data)}:
            raise ReleaseBuildError(f"runtime member hash mismatch for {platform}: {name}")

    pins = _pinned_wheels(release, platform)
    runtime_name = f"csaf-{version}-py3-none-any.whl"
    expected = {runtime_name, "requirements.lock", *(f"wheelhouse/{name}" for name in pins)}
    if set(payloads) != expected:
        raise ReleaseBuildError(f"runtime wheel closure mismatch for {platform}")
    dependencies = [_requirement(item) for item in native["dependencies"]]
    if len(dependencies) != len(pins):
        raise ReleaseBuildError(f"runtime dependency closure mismatch for {platform}")
    tag = native["platform-tags"][platform]
    lock = [f"./{runtime_name} --hash=sha256:{_hash(payloads[runtime_name])}"]
    with tempfile.TemporaryDirectory(prefix="csaf-release-verify-") as temporary:
        root = Path(temporary)
        csaf_wheel = root / runtime_name
        csaf_wheel.write_bytes(payloads[runtime_name])
        _validate_csaf_wheel(csaf_wheel, version)
        with zipfile.ZipFile(csaf_wheel) as archive:
            templates = {
                "csaf/templates/qbr/default-qbr.pptx",
                "csaf/templates/qbr/default-qbr.docx",
                "csaf/templates/qbr/provenance.json",
            }
            if not templates <= set(_wheel_members(archive, csaf_wheel)):
                raise ReleaseBuildError("CSAF wheel is missing bundled QBR templates")
        for name, dep_version, pin in dependencies:
            matches = [
                filename for filename in pins if _compatible(Path(filename), name, dep_version, tag)
            ]
            if len(matches) != 1:
                raise ReleaseBuildError(f"runtime immutable pin mismatch for {pin}")
            filename = matches[0]
            data = payloads[f"wheelhouse/{filename}"]
            if _hash(data) != pins[filename]:
                raise ReleaseBuildError(f"runtime pinned wheel hash mismatch: {filename}")
            wheel = root / filename
            wheel.write_bytes(data)
            _validate_wheel(wheel, name, dep_version, tag)
            lock.append(f"{pin} --hash=sha256:{pins[filename]}")
    if payloads["requirements.lock"] != ("\n".join(lock) + "\n").encode():
        raise ReleaseBuildError(f"runtime requirements lock mismatch for {platform}")


def verify_release(
    release_dir: Path,
    *,
    repo_root: Path,
    platform: str | None = None,
) -> tuple[str, ...]:
    """Deeply validate release assets, optionally one cross-built runtime platform."""
    directory = Path(release_dir).resolve()
    root = Path(repo_root).resolve()
    if directory.is_symlink() or not directory.is_dir():
        raise ReleaseBuildError("release directory is invalid")
    version, native, release = _project(root)
    if not re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", version):
        raise ReleaseBuildError("release version is not stable")
    platforms = (platform,) if platform else PLATFORM_IDS
    if any(item not in PLATFORM_IDS for item in platforms):
        raise ReleaseBuildError("release platform is invalid")
    runtime_names = {item: f"csaf-runtime-{item}-{version}.zip" for item in PLATFORM_IDS}
    codex_name = f"csaf-codex-skill-{version}.zip"
    claude_name = f"csaf-claude-plugin-{version}.zip"
    expected_names = {
        "csaf-release-manifest.json",
        "install.ps1",
        "install.sh",
        codex_name,
        claude_name,
        *runtime_names.values(),
    }
    _verify_outer_sums(directory, expected_names)
    for installer_name in ("install.ps1", "install.sh"):
        if (directory / installer_name).read_bytes() != _file(root / "installer" / installer_name):
            raise ReleaseBuildError(f"release installer artifact mismatch: {installer_name}")
    manifest_path = directory / "csaf-release-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = _json(manifest_path)
    if manifest_bytes != _canonical_json(manifest) or set(manifest) != {
        "schema_version",
        "version",
        "runtime",
        "codex_skill",
        "claude_plugin",
        "officecli",
    }:
        raise ReleaseBuildError("release manifest is not canonical")
    officecli = _json(root / "installer/dependencies.json").get("officecli")
    if (
        manifest["schema_version"] != 1
        or manifest["version"] != version
        or manifest["officecli"] != officecli
        or set(manifest["runtime"]) != set(PLATFORM_IDS)
    ):
        raise ReleaseBuildError("release manifest contract mismatch")
    for item, name in runtime_names.items():
        if manifest["runtime"][item] != _asset(directory / name, version):
            raise ReleaseBuildError(f"release runtime asset mismatch: {item}")
    if manifest["codex_skill"] != _asset(directory / codex_name, version):
        raise ReleaseBuildError("release Codex skill asset mismatch")
    if manifest["claude_plugin"] != _asset(directory / claude_name, version):
        raise ReleaseBuildError("release Claude plugin asset mismatch")

    expected_skill = _tree(root / "plugins/csaf/skills/csaf", "csaf")
    if _archive_payloads(directory / codex_name) != expected_skill:
        raise ReleaseBuildError("release Codex skill archive mismatch")
    expected_plugin = dict(expected_skill)
    expected_plugin[".claude-plugin/plugin.json"] = _file(
        root / "plugins/csaf/.claude-plugin/plugin.json"
    )
    if _archive_payloads(directory / claude_name) != expected_plugin:
        raise ReleaseBuildError("release Claude plugin archive mismatch")
    if _json(root / "plugins/csaf/.claude-plugin/plugin.json").get("version") != version:
        raise ReleaseBuildError("release plugin version mismatch")
    for item in platforms:
        _verify_runtime_archive(
            directory / runtime_names[item],
            platform=item,
            version=version,
            native=native,
            release=release,
        )
    return tuple(platforms)


def _acquire_build_lock(path: Path) -> int:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, bytes(1))
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise ReleaseBuildError("concurrent release build is already in progress") from exc


def _release_build_lock(descriptor: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _new_owned_staging(output: Path, version: str) -> Path:
    for _attempt in range(32):
        token = secrets.token_hex(16)
        staging = output / f".{version}.staging-{token}"
        try:
            staging.mkdir(mode=0o700)
        except FileExistsError:
            continue
        marker = _canonical_json({"schema_version": 1, "version": version, "token": token})
        marker_path = staging / ".csaf-release-staging.json"
        descriptor = os.open(
            marker_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            offset = 0
            while offset < len(marker):
                written = os.write(descriptor, marker[offset:])
                if written <= 0:
                    raise OSError("short write while creating release owner marker")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(staging)
        return staging
    raise ReleaseBuildError("could not allocate release staging directory")


def _filesystem_link_or_reparse(path: Path) -> bool:
    details = path.lstat()
    attributes = getattr(details, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse)


def _recover_owned_staging(output: Path, version: str) -> None:
    pattern = re.compile(rf"[.]{re.escape(version)}[.]staging-([0-9a-f]{{32}})")
    output_resolved = output.resolve(strict=True)
    for candidate in output.iterdir():
        match = pattern.fullmatch(candidate.name)
        if match is None:
            continue
        try:
            if _filesystem_link_or_reparse(candidate) or not candidate.is_dir():
                raise ReleaseBuildError("unsafe stale release staging directory")
            candidate_resolved = candidate.resolve(strict=True)
            candidate_resolved.relative_to(output_resolved)
            entries = list(candidate.iterdir())
            if not entries:
                shutil.rmtree(candidate)
                continue
            marker_path = candidate / ".csaf-release-staging.json"
            if marker_path not in entries:
                raise ReleaseBuildError("unsafe stale release staging directory")
            if _filesystem_link_or_reparse(marker_path) or not marker_path.is_file():
                raise ReleaseBuildError("unsafe stale release staging directory")
            expected = {"schema_version": 1, "version": version, "token": match.group(1)}
            expected_bytes = _canonical_json(expected)
            with marker_path.open("rb") as marker_file:
                marker_bytes = marker_file.read(len(expected_bytes) + 1)
            if len(marker_bytes) < len(expected_bytes) and expected_bytes.startswith(marker_bytes):
                if entries != [marker_path]:
                    raise ReleaseBuildError("unsafe stale release staging directory")
                shutil.rmtree(candidate)
                continue
            if marker_bytes != expected_bytes:
                raise ReleaseBuildError("unsafe stale release staging directory")
            for item in candidate.rglob("*"):
                item.resolve(strict=True).relative_to(candidate_resolved)
                if _filesystem_link_or_reparse(item):
                    raise ReleaseBuildError("unsafe stale release staging directory")
        except (OSError, ValueError) as exc:
            raise ReleaseBuildError("unsafe stale release staging directory") from exc
        shutil.rmtree(candidate)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            if os.name != "nt":
                raise
    finally:
        os.close(descriptor)


def _fsync_staged_tree(root: Path) -> None:
    directories: list[Path] = []
    for directory, child_directories, files in os.walk(root, topdown=False):
        current = Path(directory)
        directories.append(current)
        for name in (*child_directories, *files):
            item = current / name
            if _filesystem_link_or_reparse(item):
                raise ReleaseBuildError("release staging tree contains a link")
        for name in files:
            path = current / name
            if not path.is_file():
                raise ReleaseBuildError("release staging tree contains a non-regular file")
            flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
            descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in directories:
        _fsync_directory(directory)


def build_release(
    *,
    repo_root: Path,
    output_root: Path,
    wheel: Path,
    wheelhouse_root: Path | None = None,
    requested_version: str | None = None,
    require_clean: bool = True,
) -> Path:
    """Assemble, verify, durably stage, and atomically publish one release."""
    candidate_version = requested_version
    if candidate_version is None:
        try:
            _, candidate_version, _ = _wheel_metadata(wheel)
        except ReleaseBuildError:
            candidate_version = None
    if candidate_version is None or not re.fullmatch(
        r"[0-9]+[.][0-9]+[.][0-9]+", candidate_version
    ):
        raise ReleaseBuildError("release version is not stable")
    output = output_root.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / candidate_version
    lock = output / f".{candidate_version}.build.lock"
    staging_root: Path | None = None
    lock_descriptor: int | None = None
    published = False
    try:
        lock_descriptor = _acquire_build_lock(lock)
        _recover_owned_staging(output, candidate_version)
        if destination.exists() or destination.is_symlink():
            raise ReleaseBuildError("release destination already exists")
        staging_root = _new_owned_staging(output, candidate_version)
        staged = _assemble_release(
            repo_root=repo_root,
            output_root=staging_root,
            wheel=wheel,
            wheelhouse_root=wheelhouse_root,
            requested_version=requested_version,
            require_clean=require_clean,
        )
        verify_release(staged, repo_root=repo_root)
        _fsync_staged_tree(staging_root)
        if destination.exists() or destination.is_symlink():
            raise ReleaseBuildError("release destination already exists")
        staged.rename(destination)
        published = True
        try:
            _fsync_directory(output)
        except OSError as exc:
            raise ReleaseDurabilityError(
                "release published but durability could not be confirmed"
            ) from exc
        return destination
    finally:
        if lock_descriptor is not None:
            _release_build_lock(lock_descriptor)
        if staging_root is not None and staging_root.exists():
            shutil.rmtree(staging_root)
        if published and not destination.exists():
            raise ReleaseDurabilityError("published release disappeared before handoff")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--output", type=Path, default=Path("dist/native"))
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--verify-release", type=Path)
    parser.add_argument("--platform", choices=PLATFORM_IDS)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if args.verify_release is not None:
            verified = verify_release(args.verify_release, repo_root=root, platform=args.platform)
            print("verified release platforms: " + ",".join(verified))
            return 0
        if args.platform is not None:
            raise ReleaseBuildError("--platform requires --verify-release")
        wheels = (
            sorted((root / "dist").glob(f"csaf-{args.version or '*'}-*.whl"))
            if args.wheel is None
            else [args.wheel]
        )
        if len(wheels) != 1:
            raise ReleaseBuildError("expected exactly one CSAF wheel under dist/")
        result = build_release(
            repo_root=root,
            output_root=args.output,
            wheel=wheels[0],
            wheelhouse_root=args.wheelhouse,
            requested_version=args.version,
            require_clean=not args.allow_dirty,
        )
    except Exception:
        print("release build failed", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
