"""Safe download, persistence, and archive extraction boundaries."""

from __future__ import annotations

import bz2
import gzip
import hashlib
import hmac
import json
import lzma
import math
import os
import re
import shutil
import stat
import struct
import tarfile
import tempfile
import unicodedata
import urllib.request
import zipfile
from collections.abc import Callable
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol
from urllib.parse import urljoin, urlsplit

from csaf.setup.types import ReleaseAsset

_CHUNK_SIZE = 64 * 1024
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:[.]|$)", re.IGNORECASE
)


class SetupError(Exception):
    """A stable setup failure with explicit activation state."""

    def __init__(self, message: str, *, activated: bool = False) -> None:
        super().__init__(message)
        self.activated = activated


@dataclass(frozen=True)
class AssetLimits:
    """Hard limits applied before and while extracting an archive."""

    max_archive_bytes: int
    max_members: int
    max_member_bytes: int
    max_total_bytes: int
    max_metadata_bytes: int = 1024 * 1024
    max_path_bytes: int = 4096

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


class _Response(Protocol):
    headers: object

    def geturl(self) -> str: ...
    def read(self, size: int = -1) -> bytes: ...
    def __enter__(self) -> _Response: ...
    def __exit__(self, *args: object) -> object: ...


def _header(response: _Response, name: str) -> str | None:
    getter = getattr(response.headers, "get", None)
    return getter(name) if getter is not None else None


class _HttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow HTTPS redirects, but reject insecure locations before following."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: object,
        code: int,
        message: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirect_url = urljoin(request.full_url, newurl)
        if urlsplit(redirect_url).scheme.casefold() != "https":
            raise SetupError("asset redirect must remain HTTPS")
        return super().redirect_request(request, fp, code, message, headers, redirect_url)


def download_verified(
    asset: ReleaseAsset,
    destination: Path,
    *,
    opener: Callable[..., _Response] | None = None,
    timeout: float = 30.0,
) -> Path:
    """Stream an HTTPS asset to a private file and activate it after verification."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise SetupError("download timeout must be finite and positive")
    target = Path(destination)
    temporary: Path | None = None
    try:
        _prepare_private_parent(target.parent)
        request = urllib.request.Request(str(asset.url), headers={"Accept-Encoding": "identity"})
        response_context = (
            urllib.request.build_opener(_HttpsRedirectHandler()).open(request, timeout=timeout)
            if opener is None
            else opener(request, timeout=timeout)
        )
        with response_context as response:
            if urlsplit(response.geturl()).scheme.casefold() != "https":
                raise SetupError("asset redirect must remain HTTPS")
            content_length = _header(response, "Content-Length")
            if content_length is not None:
                try:
                    response_size = int(content_length)
                except ValueError as error:
                    raise SetupError("invalid asset size header") from error
                if response_size != asset.size:
                    raise SetupError("asset size header does not match declared size")
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
            )
            temporary = Path(temporary_name)
            digest = hashlib.sha256()
            written = 0
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    chunk = response.read(min(_CHUNK_SIZE, asset.size - written + 1))
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > asset.size:
                        raise SetupError("download exceeded declared size")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if written != asset.size:
                raise SetupError("download did not match declared size")
            if not hmac.compare_digest(digest.hexdigest(), asset.sha256):
                raise SetupError("download SHA-256 did not match")
        _activate_temp_file(temporary, target)
        temporary = None
        return target
    except SetupError:
        raise
    except (OSError, ValueError) as error:
        raise SetupError("asset download failed") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def write_json_atomic(path: Path, value: object) -> None:
    """Write deterministic UTF-8 JSON through a private sibling temporary file."""
    target = Path(path)
    temporary: Path | None = None
    try:
        _prepare_private_parent(target.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        _activate_temp_file(temporary, target)
        temporary = None
    except (OSError, TypeError, ValueError) as error:
        raise SetupError("could not write UTF-8 JSON") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def read_json(path: Path) -> object:
    """Read a JSON document using strict UTF-8 decoding."""
    try:
        with Path(path).open("r", encoding="utf-8", errors="strict") as source:
            return json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SetupError("could not read UTF-8 JSON") from error


@dataclass(frozen=True)
class _Member:
    name: str
    size: int
    is_directory: bool
    executable: bool
    source: object


def _safe_member_name(raw_name: str, *, is_directory: bool) -> str:
    if not raw_name or "\\" in raw_name or _DRIVE_PREFIX.match(raw_name):
        raise SetupError(f"unsafe archive member: {raw_name!r}")
    if any(unicodedata.category(character) == "Cc" for character in raw_name):
        raise SetupError(f"unsafe archive member: {raw_name!r}")
    name = raw_name[:-1] if is_directory and raw_name.endswith("/") else raw_name
    if not name or name.startswith("/") or "//" in name:
        raise SetupError(f"unsafe archive member: {raw_name!r}")
    raw_parts = name.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise SetupError(f"unsafe archive member: {raw_name!r}")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SetupError(f"unsafe archive member: {raw_name!r}")
    for part in path.parts:
        if (
            any(character in '<>:"|?*' for character in part)
            or part.endswith((".", " "))
            or _WINDOWS_RESERVED.match(part)
        ):
            raise SetupError(f"unsafe archive member: {raw_name!r}")
    return path.as_posix()


def _zip_members(archive: zipfile.ZipFile) -> list[_Member]:
    members: list[_Member] = []
    for info in archive.infolist():
        if archive.fp is None:
            raise SetupError("could not inspect zip member table")
        position = archive.fp.tell()
        archive.fp.seek(info.header_offset)
        header = archive.fp.read(30)
        if len(header) != 30:
            raise SetupError(f"unsupported archive member: {info.filename!r}")
        values = struct.unpack("<4s5H3L2H", header)
        raw_name = archive.fp.read(values[-2])
        archive.fp.seek(position)
        if len(raw_name) != values[-2] or any(byte < 32 or byte == 127 for byte in raw_name):
            raise SetupError(f"unsafe archive member: {info.filename!r}")
        if bytes((92,)) in raw_name:
            raise SetupError(f"unsafe archive member: {info.filename!r}")
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR} or stat.S_ISLNK(mode):
            raise SetupError(f"unsupported archive member: {info.filename!r}")
        if info.flag_bits & 0x1:
            raise SetupError(f"unsupported archive member: {info.filename!r}")
        is_directory = info.is_dir() or file_type == stat.S_IFDIR
        members.append(
            _Member(
                _safe_member_name(info.filename, is_directory=is_directory),
                info.file_size,
                is_directory,
                bool(mode & 0o111),
                info,
            )
        )
    return members


def _read_tar_bytes(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(min(_CHUNK_SIZE, remaining))
        if not chunk:
            raise SetupError("invalid TAR metadata")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _discard_tar_bytes(stream: BinaryIO, size: int) -> None:
    remaining = size
    while remaining:
        chunk = stream.read(min(_CHUNK_SIZE, remaining))
        if not chunk:
            raise SetupError("invalid TAR metadata")
        remaining -= len(chunk)


def _parse_pax_metadata(payload: bytes, limits: AssetLimits) -> None:
    position = 0
    while position < len(payload):
        separator = payload.find(b" ", position)
        if separator < 0:
            raise SetupError("invalid TAR metadata")
        try:
            record_size = int(payload[position:separator])
        except ValueError as error:
            raise SetupError("invalid TAR metadata") from error
        if record_size <= 0 or position + record_size > len(payload):
            raise SetupError("invalid TAR metadata")
        record = payload[separator + 1 : position + record_size]
        if not record.endswith(b"\n") or b"=" not in record:
            raise SetupError("invalid TAR metadata")
        key, value = record[:-1].split(b"=", 1)
        if key.startswith(b"GNU.sparse"):
            raise SetupError("unsupported TAR sparse metadata")
        if key in {b"path", b"linkpath"} and len(value) > limits.max_path_bytes:
            raise SetupError("TAR path exceeds limit")
        position += record_size


def _tar_preflight_stream(secured_archive: BinaryIO) -> BinaryIO:
    secured_archive.seek(0)
    magic = secured_archive.read(6)
    secured_archive.seek(0)
    if magic.startswith(b"\x1f\x8b"):
        return gzip.GzipFile(fileobj=secured_archive, mode="rb")
    if magic.startswith(b"BZh"):
        return bz2.BZ2File(secured_archive, mode="rb")
    if magic.startswith(b"\xfd7zXZ\x00"):
        return lzma.LZMAFile(secured_archive, mode="rb")
    return secured_archive


def _preflight_tar(secured_archive: BinaryIO, limits: AssetLimits) -> None:
    stream = _tar_preflight_stream(secured_archive)
    context = nullcontext(stream) if stream is secured_archive else closing(stream)
    metadata_total = 0
    member_count = 0
    content_total = 0
    with context as source:
        while True:
            header = _read_tar_bytes(source, 512)
            if header == bytes(512):
                break
            try:
                size = tarfile.nti(header[124:136])
            except (ValueError, OverflowError) as error:
                raise SetupError("invalid TAR metadata") from error
            if size is None or size < 0:
                raise SetupError("invalid TAR metadata")
            type_flag = header[156:157] or tarfile.REGTYPE
            name = header[0:100].split(b"\0", 1)[0]
            prefix = header[345:500].split(b"\0", 1)[0]
            raw_path = prefix + (b"/" if prefix and name else b"") + name
            if len(raw_path) > limits.max_path_bytes:
                raise SetupError("TAR path exceeds limit")
            is_metadata = type_flag in {
                tarfile.XHDTYPE,
                tarfile.XGLTYPE,
                tarfile.GNUTYPE_LONGNAME,
                tarfile.GNUTYPE_LONGLINK,
            }
            if type_flag == tarfile.GNUTYPE_SPARSE:
                raise SetupError("unsupported TAR sparse metadata")
            padding = (-size) % 512
            if is_metadata:
                metadata_size = 512 + size + padding
                metadata_total += metadata_size
                if (
                    metadata_size > limits.max_metadata_bytes
                    or metadata_total > limits.max_metadata_bytes
                ):
                    raise SetupError("TAR metadata exceeds limit")
                payload = _read_tar_bytes(source, size)
                if type_flag in {tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK}:
                    if len(payload.rstrip(b"\0")) > limits.max_path_bytes:
                        raise SetupError("TAR path exceeds limit")
                else:
                    _parse_pax_metadata(payload, limits)
            else:
                member_count += 1
                if member_count > limits.max_members:
                    raise SetupError("archive member count exceeds limit")
                if size > limits.max_member_bytes:
                    raise SetupError("archive member size exceeds limit")
                if type_flag in {tarfile.REGTYPE, tarfile.AREGTYPE}:
                    content_total += size
                    if content_total > limits.max_total_bytes:
                        raise SetupError("archive total size exceeds limit")
                _discard_tar_bytes(source, size)
            if padding:
                _discard_tar_bytes(source, padding)
    secured_archive.seek(0)


def _tar_members(archive: tarfile.TarFile, limits: AssetLimits) -> list[_Member]:
    members: list[_Member] = []
    total = 0
    for info in archive:
        if len(members) >= limits.max_members:
            raise SetupError("archive member count exceeds limit")
        if not (info.isfile() or info.isdir()):
            raise SetupError(f"unsupported archive member: {info.name!r}")
        if info.size < 0 or info.size > limits.max_member_bytes:
            raise SetupError(f"archive member size exceeds limit: {info.name!r}")
        if info.isfile():
            total += info.size
            if total > limits.max_total_bytes:
                raise SetupError("archive total size exceeds limit")
        members.append(
            _Member(
                _safe_member_name(info.name, is_directory=info.isdir()),
                info.size,
                info.isdir(),
                bool(info.mode & 0o111),
                info,
            )
        )
    return members


def _validate_members(members: list[_Member], limits: AssetLimits) -> list[_Member]:
    if len(members) > limits.max_members:
        raise SetupError("archive member count exceeds limit")
    seen: dict[str, _Member] = {}
    total = 0
    for member in members:
        folded = unicodedata.normalize("NFC", member.name).casefold()
        if folded in seen:
            raise SetupError(f"duplicate or case-colliding archive member: {member.name!r}")
        seen[folded] = member
        if member.size < 0 or member.size > limits.max_member_bytes:
            raise SetupError(f"archive member size exceeds limit: {member.name!r}")
        if not member.is_directory:
            total += member.size
            if total > limits.max_total_bytes:
                raise SetupError("archive total size exceeds limit")
    files = {name for name, member in seen.items() if not member.is_directory}
    for folded, member in seen.items():
        parts = folded.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in files:
                raise SetupError(f"conflicting archive member: {member.name!r}")
    return sorted(members, key=lambda member: member.name)


def _reject_linked_parents(path: Path) -> None:
    absolute = path.absolute()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for component in reversed((absolute, *absolute.parents)):
        try:
            details = component.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(details, "st_file_attributes", 0)
        if stat.S_ISLNK(details.st_mode) or attributes & reparse_flag:
            raise SetupError("activation parent contains a symlink or reparse point")


def _prepare_private_parent(parent: Path) -> None:
    """Require the activation parent to be CSAF-owned and private.

    This prevents accidental cross-user interference. No portable sequence can
    defeat a malicious same-user process that may mutate the directory between
    syscalls, so callers must keep this directory within CSAF-owned state.
    """
    _reject_linked_parents(parent)
    existed = parent.exists()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        os.chmod(parent, 0o700)
    _reject_linked_parents(parent)
    if os.name == "posix":
        details = parent.lstat()
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise SetupError("activation parent must be CSAF-owned and private")
        if hasattr(os, "geteuid") and details.st_uid != os.geteuid():
            raise SetupError("activation parent must be CSAF-owned and private")


def _open_directory_fd(parent: Path) -> int | None:
    if not hasattr(os, "O_DIRECTORY"):
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    return os.open(parent, flags)


def _fsync_directory(parent: Path) -> None:
    descriptor = _open_directory_fd(parent)
    if descriptor is None:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _activate_temp_file(temporary: Path, target: Path) -> None:
    _reject_linked_parents(target.parent)
    descriptor = _open_directory_fd(target.parent)
    activated = False
    try:
        os.replace(temporary, target)
        activated = True
        if descriptor is not None:
            os.fsync(descriptor)
    except OSError as error:
        if activated:
            raise SetupError(
                "activation completed but durability is uncertain",
                activated=True,
            ) from error
        raise
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _fsync_staged_directories(root: Path) -> None:
    """Persist staged directory entries on platforms with directory descriptors."""
    for directory, _children, _files in os.walk(root, topdown=False):
        _fsync_directory(Path(directory))


def _copy_bounded(source: BinaryIO, output: BinaryIO, expected: int, name: str) -> None:
    written = 0
    while True:
        chunk = source.read(min(_CHUNK_SIZE, expected - written + 1))
        if not chunk:
            break
        written += len(chunk)
        if written > expected:
            raise SetupError(f"archive member exceeded declared size: {name!r}")
        output.write(chunk)
    if written != expected:
        raise SetupError(f"archive member did not match declared size: {name!r}")


def _acquire_activation_lock(path: Path) -> int:
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
    except OSError as error:
        os.close(descriptor)
        raise SetupError("archive extraction already in progress") from error


def _release_activation_lock(descriptor: int) -> None:
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


def extract_verified_archive(
    archive: Path,
    destination: Path,
    *,
    limits: AssetLimits,
) -> tuple[Path, ...]:
    """Validate, privately stage, and atomically activate an archive.

    The activation parent must be CSAF-owned and private. This boundary reduces
    filesystem races but cannot protect against a malicious same-user process
    allowed to mutate that directory between syscalls.
    """
    archive_path = Path(archive)
    target = Path(destination)
    temporary: Path | None = None
    lock_path = target.parent / f".{target.name}.lock"
    lock_descriptor: int | None = None
    try:
        _prepare_private_parent(target.parent)
        lock_descriptor = _acquire_activation_lock(lock_path)

        _reject_linked_parents(target.parent)
        if target.exists() or target.is_symlink():
            raise SetupError("archive destination already exists")

        with archive_path.open("rb") as secured_archive:
            if os.fstat(secured_archive.fileno()).st_size > limits.max_archive_bytes:
                raise SetupError("compressed archive size exceeds limit")
            secured_archive.seek(0)
            if zipfile.is_zipfile(secured_archive):
                secured_archive.seek(0)
                archive_context: zipfile.ZipFile | tarfile.TarFile = zipfile.ZipFile(
                    secured_archive
                )
                kind = "zip"
            else:
                secured_archive.seek(0)
                try:
                    _preflight_tar(secured_archive, limits)
                    secured_archive.seek(0)
                    archive_context = tarfile.open(fileobj=secured_archive, mode="r:*")
                except tarfile.ReadError as error:
                    raise SetupError("unsupported archive format") from error
                kind = "tar"

            with archive_context as opened:
                raw_members = (
                    _zip_members(opened) if kind == "zip" else _tar_members(opened, limits)
                )
                members = _validate_members(raw_members, limits)
                temporary = Path(
                    tempfile.mkdtemp(
                        dir=target.parent,
                        prefix=f".{target.name}.",
                        suffix=".tmp",
                    )
                )
                os.chmod(temporary, 0o700)
                extracted_names: list[str] = []
                for member in members:
                    output_path = temporary.joinpath(*member.name.split("/"))
                    if member.is_directory:
                        output_path.mkdir(parents=True, exist_ok=True)
                        os.chmod(output_path, 0o700)
                        continue
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    os.chmod(output_path.parent, 0o700)
                    if kind == "zip":
                        source_context = opened.open(member.source, "r")
                    else:
                        source = opened.extractfile(member.source)
                        if source is None:
                            raise SetupError(f"could not read archive member: {member.name!r}")
                        source_context = closing(source)
                    with source_context as source, output_path.open("xb") as output:
                        _copy_bounded(source, output, member.size, member.name)
                        os.chmod(output_path, 0o700 if member.executable else 0o600)
                        output.flush()
                        os.fsync(output.fileno())
                    extracted_names.append(member.name)

        _fsync_staged_directories(temporary)
        _reject_linked_parents(target.parent)
        directory_descriptor = _open_directory_fd(target.parent)
        try:
            if target.exists() or target.is_symlink():
                raise SetupError("archive destination already exists")
            os.replace(temporary, target)
            if directory_descriptor is not None:
                try:
                    os.fsync(directory_descriptor)
                except OSError as error:
                    raise SetupError(
                        "activation completed but durability is uncertain",
                        activated=True,
                    ) from error
        finally:
            if directory_descriptor is not None:
                try:
                    os.close(directory_descriptor)
                except OSError:
                    pass
        temporary = None
        return tuple(target.joinpath(*name.split("/")) for name in extracted_names)
    except SetupError:
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile, RuntimeError) as error:
        raise SetupError("archive extraction failed") from error
    finally:
        if temporary is not None:
            try:
                shutil.rmtree(temporary)
            except OSError:
                pass
        if lock_descriptor is not None:
            try:
                _release_activation_lock(lock_descriptor)
            except OSError:
                pass
