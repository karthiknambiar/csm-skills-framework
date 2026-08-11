from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import time
import warnings
import zipfile
from email.message import Message
from pathlib import Path
from urllib.request import Request

import pytest

import csaf.setup.assets as assets_module
from csaf.setup import ReleaseAsset, SetupError
from csaf.setup.assets import (
    AssetLimits,
    _HttpsRedirectHandler,
    download_verified,
    extract_verified_archive,
    read_json,
    write_json_atomic,
)

TEST_LIMITS = AssetLimits(
    max_archive_bytes=1024 * 1024,
    max_members=20,
    max_member_bytes=1024,
    max_total_bytes=4096,
)


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str = "https://example.test/asset",
        content_length: int | None = None,
        fail_after: int | None = None,
    ) -> None:
        self._body = io.BytesIO(body)
        self._url = url
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}
        self.fail_after = fail_after
        self.read_sizes: list[int] = []
        self.total_read = 0

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        assert size > 0
        self.read_sizes.append(size)
        if self.fail_after is not None and self.total_read >= self.fail_after:
            raise OSError("network failed")
        chunk = self._body.read(size)
        self.total_read += len(chunk)
        return chunk


def _asset(body: bytes, *, size: int | None = None, sha256: str | None = None) -> ReleaseAsset:
    return ReleaseAsset(
        url="https://example.test/asset",
        sha256=sha256 or hashlib.sha256(body).hexdigest(),
        size=len(body) if size is None else size,
    )


def _zip(path: Path, members: list[tuple[str, bytes]]) -> Path:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as archive:
            for name, body in members:
                archive.writestr(name, body)
    return path


def _zip_with_raw_name(path: Path, safe_name: str, raw_name: bytes) -> Path:
    assert len(safe_name.encode()) == len(raw_name)
    _zip(path, [(safe_name, b"secret")])
    original = safe_name.encode()
    payload = path.read_bytes()
    assert payload.count(original) == 2
    path.write_bytes(payload.replace(original, raw_name))
    return path


def _tar(path: Path, members: list[tuple[str, bytes]]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, body in members:
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return path


def test_download_streams_and_atomically_replaces_after_verification(tmp_path: Path) -> None:
    body = b"verified payload"
    response = FakeResponse(body, content_length=len(body))
    destination = tmp_path / "asset.bin"

    result = download_verified(_asset(body), destination, opener=lambda *_, **__: response)

    assert result == destination
    assert destination.read_bytes() == body
    assert response.read_sizes and all(size > 0 for size in response.read_sizes)
    assert not list(tmp_path.glob(".asset.bin.*.tmp"))


def test_download_passes_finite_default_timeout_to_opener(tmp_path: Path) -> None:
    body = b"payload"
    observed: list[float] = []

    def opener(*args: object, timeout: float) -> FakeResponse:
        observed.append(timeout)
        return FakeResponse(body)

    download_verified(_asset(body), tmp_path / "asset", opener=opener)
    assert observed == [30.0]


def test_download_honors_configured_timeout(tmp_path: Path) -> None:
    body = b"payload"
    observed: list[float] = []

    def opener(*args: object, timeout: float) -> FakeResponse:
        observed.append(timeout)
        return FakeResponse(body)

    download_verified(_asset(body), tmp_path / "asset", opener=opener, timeout=7.5)
    assert observed == [7.5]


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_download_rejects_nonfinite_or_nonpositive_timeout(tmp_path: Path, timeout: float) -> None:
    with pytest.raises(SetupError, match="timeout"):
        download_verified(_asset(b"x"), tmp_path / "asset", timeout=timeout)


def test_redirect_handler_rejects_insecure_location_before_following() -> None:
    handler = _HttpsRedirectHandler()
    request = Request("https://github.example/release")
    with pytest.raises(SetupError, match="HTTPS"):
        handler.redirect_request(request, None, 302, "Found", Message(), "http://cdn.example/file")


def test_redirect_handler_allows_https_cross_origin() -> None:
    handler = _HttpsRedirectHandler()
    request = Request("https://github.example/release")
    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        Message(),
        "https://cdn.example/file",
    )
    assert redirected is not None
    assert redirected.full_url == "https://cdn.example/file"


def test_download_rejects_redirect_to_non_https(tmp_path: Path) -> None:
    body = b"payload"
    response = FakeResponse(body, url="http://example.test/asset")
    with pytest.raises(SetupError, match="HTTPS"):
        download_verified(_asset(body), tmp_path / "asset", opener=lambda *_, **__: response)
    assert response.total_read == 0


@pytest.mark.parametrize("declared", [1, 99])
def test_download_checks_content_length_before_reading(tmp_path: Path, declared: int) -> None:
    body = b"payload"
    response = FakeResponse(body, content_length=declared)
    with pytest.raises(SetupError, match="size"):
        download_verified(_asset(body), tmp_path / "asset", opener=lambda *_, **__: response)
    assert response.total_read == 0


@pytest.mark.parametrize(
    ("asset", "response"),
    [
        (_asset(b"payload", size=3), FakeResponse(b"payload")),
        (_asset(b"payload", size=12), FakeResponse(b"payload")),
        (_asset(b"payload", sha256="0" * 64), FakeResponse(b"payload")),
        (_asset(b"payload"), FakeResponse(b"payload", fail_after=1)),
    ],
)
def test_download_failure_cleans_temporary_file_and_preserves_destination(
    tmp_path: Path,
    asset: ReleaseAsset,
    response: FakeResponse,
) -> None:
    destination = tmp_path / "asset"
    destination.write_bytes(b"old")
    with pytest.raises(SetupError):
        download_verified(asset, destination, opener=lambda *_, **__: response)
    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.glob(".asset.*.tmp"))


def test_download_uses_private_temporary_file(tmp_path: Path) -> None:
    body = b"payload"
    observed_modes: list[int] = []

    class InspectingResponse(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            temporary = next(tmp_path.glob(".asset.*.tmp"))
            observed_modes.append(stat.S_IMODE(temporary.stat().st_mode))
            return super().read(size)

    download_verified(
        _asset(body), tmp_path / "asset", opener=lambda *_, **__: InspectingResponse(body)
    )
    assert observed_modes
    if os.name != "nt":
        assert all(mode & 0o077 == 0 for mode in observed_modes)


def test_download_wraps_parent_creation_failure_without_leaking_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SECRET C:/customer/path"
    monkeypatch.setattr(
        Path, "mkdir", lambda *args, **kwargs: (_ for _ in ()).throw(OSError(secret))
    )
    with pytest.raises(SetupError) as raised:
        download_verified(_asset(b"x"), tmp_path / "new" / "asset", opener=lambda *a, **k: None)
    assert secret not in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)


def test_download_redacts_network_exception_details(tmp_path: Path) -> None:
    secret = "SECRET https://token@example.test/private"

    def opener(*args: object, **kwargs: object) -> FakeResponse:
        raise OSError(secret)

    with pytest.raises(SetupError) as raised:
        download_verified(_asset(b"x"), tmp_path / "asset", opener=opener)
    assert secret not in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)


def test_json_parent_creation_failure_is_wrapped_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SECRET C:/state/path"
    monkeypatch.setattr(
        Path, "mkdir", lambda *args, **kwargs: (_ for _ in ()).throw(OSError(secret))
    )
    with pytest.raises(SetupError) as raised:
        write_json_atomic(tmp_path / "new" / "state.json", {})
    assert secret not in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)


def test_json_directory_fsync_failure_is_redacted_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SECRET directory failure"
    monkeypatch.setattr(assets_module, "_open_directory_fd", lambda parent: 123)

    def fsync(descriptor: int) -> None:
        if descriptor == 123:
            raise OSError(secret)

    monkeypatch.setattr(assets_module.os, "fsync", fsync)
    monkeypatch.setattr(assets_module.os, "close", lambda descriptor: None)
    with pytest.raises(SetupError) as raised:
        write_json_atomic(tmp_path / "state.json", {})
    assert secret not in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)
    assert raised.value.activated is True
    assert read_json(tmp_path / "state.json") == {}
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_json_precommit_failure_preserves_target_and_reports_not_activated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"old":true}', encoding="utf-8")
    secret = "SECRET replace failure"
    monkeypatch.setattr(
        assets_module.os,
        "replace",
        lambda *args: (_ for _ in ()).throw(OSError(secret)),
    )
    with pytest.raises(SetupError) as raised:
        write_json_atomic(target, {"new": True})
    assert raised.value.activated is False
    assert secret not in str(raised.value)
    assert read_json(target) == {"old": True}
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_json_round_trip_is_atomic_utf8(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    value = {"message": "café", "items": [1, True, None]}
    write_json_atomic(path, value)
    assert read_json(path) == value
    assert json.loads(path.read_text(encoding="utf-8")) == value
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_json_read_is_strict_utf8(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b'{"bad":"\xff"}')
    with pytest.raises(SetupError, match="UTF-8 JSON"):
        read_json(path)


@pytest.mark.parametrize("builder", [_zip, _tar])
def test_extracts_zip_and_tar_in_sorted_order(tmp_path: Path, builder: object) -> None:
    archive = builder(
        tmp_path / ("asset.zip" if builder is _zip else "asset.tar.gz"),
        [("z.txt", b"z"), ("dir/a.txt", b"a")],
    )
    extracted = extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert extracted == (tmp_path / "out" / "dir" / "a.txt", tmp_path / "out" / "z.txt")
    assert [path.read_bytes() for path in extracted] == [b"a", b"z"]


@pytest.mark.parametrize(
    "name",
    [
        "/absolute",
        "C:/drive",
        "C:\\drive",
        "../escape",
        "dir/../escape",
        "control\x1fname",
    ],
)
def test_extract_rejects_unsafe_names_before_writing(tmp_path: Path, name: str) -> None:
    archive = _zip(tmp_path / "bad.zip", [(name, b"secret")])
    with pytest.raises(SetupError, match="unsafe archive member"):
        extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("builder", [_zip, _tar])
@pytest.mark.parametrize(
    "name",
    [
        "nested/has:stream",
        "dir/./file",
        "has<angle",
        "has>angle",
        'has"quote',
        "has|pipe",
        "has?question",
        "has*star",
        "trailing.",
        "trailing ",
        "CON",
        "prn.txt",
        "nested/COM9.log",
        "LPT1",
        "COM¹",
        "lpt².txt",
        "nested/COM³.log",
    ],
)
def test_extract_rejects_windows_unsafe_components_on_every_host(
    tmp_path: Path,
    builder: object,
    name: str,
) -> None:
    suffix = "zip" if builder is _zip else "tar.gz"
    archive = builder(tmp_path / f"bad.{suffix}", [(name, b"secret")])
    with pytest.raises(SetupError, match="unsafe archive member"):
        extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("builder", [_zip, _tar])
def test_extract_rejects_unicode_control_names(tmp_path: Path, builder: object) -> None:
    suffix = "zip" if builder is _zip else "tar.gz"
    archive = builder(tmp_path / f"bad.{suffix}", [("bad\u0085name", b"secret")])
    with pytest.raises(SetupError, match="unsafe archive member"):
        extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("raw_name", [rb"dir\escape", rb"mix\slash/"])
def test_extract_rejects_raw_zip_backslashes(tmp_path: Path, raw_name: bytes) -> None:
    archive = _zip_with_raw_name(tmp_path / "bad.zip", "x" * len(raw_name), raw_name)
    with pytest.raises(SetupError, match="unsafe archive member"):
        extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert not (tmp_path / "out").exists()


def test_extract_rejects_raw_zip_nul(tmp_path: Path) -> None:
    archive = _zip_with_raw_name(tmp_path / "bad.zip", "nulXname", b"nul\x00name")
    with pytest.raises(SetupError, match="unsafe archive member"):
        extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("builder", [_zip, _tar])
def test_extract_rejects_unicode_normalization_collisions(
    tmp_path: Path,
    builder: object,
) -> None:
    suffix = "zip" if builder is _zip else "tar.gz"
    archive = builder(
        tmp_path / f"bad.{suffix}",
        [("café", b"a"), ("cafe\u0301", b"b")],
    )
    with pytest.raises(SetupError, match="case-colliding archive member"):
        extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "members",
    [
        [("same", b"a"), ("same", b"b")],
        [("Readme", b"a"), ("README", b"b")],
        [("node", b"a"), ("node/child", b"b")],
    ],
)
def test_extract_rejects_duplicate_case_colliding_and_conflicting_members(
    tmp_path: Path,
    members: list[tuple[str, bytes]],
) -> None:
    archive = _zip(tmp_path / "bad.zip", members)
    with pytest.raises(SetupError, match="archive member"):
        extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert not (tmp_path / "out").exists()


def test_extract_fsyncs_staged_regular_files_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip(tmp_path / "asset.zip", [("file", b"data")])
    original_fsync = assets_module.os.fsync
    regular_fsyncs = 0

    def recording_fsync(descriptor: int) -> None:
        nonlocal regular_fsyncs
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            regular_fsyncs += 1
        original_fsync(descriptor)

    monkeypatch.setattr(assets_module.os, "fsync", recording_fsync)
    extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert regular_fsyncs >= 1


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_extract_applies_private_modes_from_safe_execute_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    archive_path = tmp_path / ("modes.zip" if kind == "zip" else "modes.tar")
    if kind == "zip":
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name, mode, body in (
                ("dir/", stat.S_IFDIR | 0o777, b""),
                ("plain", stat.S_IFREG | 0o666, b"p"),
                ("run", stat.S_IFREG | 0o775, b"x"),
            ):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, body)
    else:
        with tarfile.open(archive_path, "w") as archive:
            directory = tarfile.TarInfo("dir")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o777
            archive.addfile(directory)
            for name, mode, body in (("plain", 0o666, b"p"), ("run", 0o775, b"x")):
                info = tarfile.TarInfo(name)
                info.mode = mode
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        assets_module.os,
        "chmod",
        lambda path, mode: calls.append((Path(path).name, mode)),
    )
    extract_verified_archive(archive_path, tmp_path / "out", limits=TEST_LIMITS)

    assert ("dir", 0o700) in calls
    assert ("plain", 0o600) in calls
    assert ("run", 0o700) in calls


def test_extract_rejects_zip_symlink(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")
    with pytest.raises(SetupError, match="unsupported archive member"):
        extract_verified_archive(archive_path, tmp_path / "out", limits=TEST_LIMITS)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "device"])
def test_extract_rejects_unsafe_tar_member_types(tmp_path: Path, kind: str) -> None:
    archive_path = tmp_path / "bad.tar"
    with tarfile.open(archive_path, "w") as archive:
        info = tarfile.TarInfo("bad")
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "target"
        elif kind == "hardlink":
            info.type = tarfile.LNKTYPE
            info.linkname = "target"
        else:
            info.type = tarfile.CHRTYPE
        archive.addfile(info)
    with pytest.raises(SetupError, match="unsupported archive member"):
        extract_verified_archive(archive_path, tmp_path / "out", limits=TEST_LIMITS)


@pytest.mark.parametrize(
    ("limits", "members", "message"),
    [
        (AssetLimits(10, 20, 1024, 4096), [("a", b"x")], "archive size"),
        (AssetLimits(1024 * 1024, 1, 1024, 4096), [("a", b"x"), ("b", b"y")], "member count"),
        (AssetLimits(1024 * 1024, 20, 2, 4096), [("a", b"xxx")], "member size"),
        (AssetLimits(1024 * 1024, 20, 1024, 4), [("a", b"xxx"), ("b", b"yyy")], "total size"),
    ],
)
def test_extract_enforces_all_limits_before_writing(
    tmp_path: Path,
    limits: AssetLimits,
    members: list[tuple[str, bytes]],
    message: str,
) -> None:
    archive = _zip(tmp_path / "asset.zip", members)
    with pytest.raises(SetupError, match=message):
        extract_verified_archive(archive, tmp_path / "out", limits=limits)
    assert not (tmp_path / "out").exists()


def _metadata_tar(path: Path, kind: str) -> Path:
    if kind == "pax":
        with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo("file")
            info.pax_headers = {"path": "a" * 100_000}
            archive.addfile(info)
    elif kind == "global-pax":
        with tarfile.open(
            path,
            "w:gz",
            format=tarfile.PAX_FORMAT,
            pax_headers={"comment": "x" * 100_000},
        ):
            pass
    elif kind == "longname":
        with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as archive:
            archive.addfile(tarfile.TarInfo("a" * 10_000))
    else:
        with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as archive:
            info = tarfile.TarInfo("sparse")
            info.type = tarfile.GNUTYPE_SPARSE
            archive.addfile(info)
    return path


class _RecordingTarStream:
    def __init__(self, source: io.BufferedReader) -> None:
        self.source = source
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.source.read(size)

    def close(self) -> None:
        pass


def test_tar_preflight_discards_regular_bodies_without_materializing_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "large.tar"
    body = b"x" * (assets_module._CHUNK_SIZE * 2 + 7)
    with tarfile.open(archive, "w") as output:
        info = tarfile.TarInfo("file")
        info.size = len(body)
        output.addfile(info, io.BytesIO(body))
    original_read = assets_module._read_tar_bytes
    recording: _RecordingTarStream | None = None

    def record_stream(source: io.BufferedReader) -> _RecordingTarStream:
        nonlocal recording
        recording = _RecordingTarStream(source)
        return recording

    def metadata_reads_only(stream: object, size: int) -> bytes:
        assert size <= 512, "regular bodies must not use the materializing reader"
        return original_read(stream, size)

    monkeypatch.setattr(assets_module, "_tar_preflight_stream", record_stream)
    monkeypatch.setattr(assets_module, "_read_tar_bytes", metadata_reads_only)
    limits = AssetLimits(1024 * 1024, 20, 256 * 1024, 256 * 1024)
    with archive.open("rb") as source:
        assets_module._preflight_tar(source, limits)

    assert recording is not None
    assert recording.read_sizes
    assert all(0 < size <= assets_module._CHUNK_SIZE for size in recording.read_sizes)


@pytest.mark.parametrize(
    "type_flag",
    [
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    ],
)
def test_tar_preflight_charges_zero_payload_metadata_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    type_flag: bytes,
) -> None:
    header = tarfile.TarInfo("metadata")
    header.type = type_flag
    archive = tmp_path / "metadata-flood.tar"
    archive.write_bytes(header.tobuf() * 10 + bytes(1024))
    recording: _RecordingTarStream | None = None

    def record_stream(source: io.BufferedReader) -> _RecordingTarStream:
        nonlocal recording
        recording = _RecordingTarStream(source)
        return recording

    monkeypatch.setattr(assets_module, "_tar_preflight_stream", record_stream)
    monkeypatch.setattr(
        tarfile,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("metadata reached TarInfo materialization")
        ),
    )
    limits = AssetLimits(
        max_archive_bytes=1024 * 1024,
        max_members=20,
        max_member_bytes=1024,
        max_total_bytes=4096,
        max_metadata_bytes=1024,
    )

    with pytest.raises(SetupError, match="^TAR metadata exceeds limit$"):
        extract_verified_archive(archive, tmp_path / "out", limits=limits)
    assert recording is not None
    assert len(recording.read_sizes) <= 3
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("kind", ["pax", "global-pax", "longname", "sparse"])
def test_extract_rejects_bounded_or_sparse_tar_metadata_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    archive = _metadata_tar(tmp_path / f"{kind}.tar", kind)
    limits = AssetLimits(
        max_archive_bytes=1024 * 1024,
        max_members=20,
        max_member_bytes=1024,
        max_total_bytes=4096,
        max_metadata_bytes=256,
        max_path_bytes=64,
    )
    materialized = False
    original = tarfile.TarInfo._proc_pax

    def record_materialization(*args: object, **kwargs: object) -> object:
        nonlocal materialized
        materialized = True
        return original(*args, **kwargs)

    monkeypatch.setattr(tarfile.TarInfo, "_proc_pax", record_materialization)
    with pytest.raises(SetupError, match="metadata|path|sparse"):
        extract_verified_archive(archive, tmp_path / "out", limits=limits)
    assert materialized is False
    assert not (tmp_path / "out").exists()


def test_extract_uses_open_handle_size_instead_of_path_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip(tmp_path / "asset.zip", [("file", b"data")])
    original_stat = Path.stat

    def guarded_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        if path == archive:
            raise AssertionError("archive path stat must not be used")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    extracted = extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert extracted[0].read_bytes() == b"data"


def test_tar_member_limit_stops_incremental_scan_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "bomb.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for index in range(10_000):
            archive.addfile(tarfile.TarInfo(f"member-{index:05d}"))
    calls = 0
    original_next = tarfile.TarFile.next

    def counted_next(archive: tarfile.TarFile) -> tarfile.TarInfo | None:
        nonlocal calls
        calls += 1
        return original_next(archive)

    monkeypatch.setattr(tarfile.TarFile, "next", counted_next)
    limits = AssetLimits(20 * 1024 * 1024, 2, 1024, 4096)
    with pytest.raises(SetupError, match="member count"):
        extract_verified_archive(archive_path, tmp_path / "out", limits=limits)
    assert calls <= 4  # initial TAR probe plus at most max_members + 1 entries
    assert not (tmp_path / "out").exists()


def test_stale_lock_marker_does_not_block_extraction(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "asset.zip", [("file", b"data")])
    lock = tmp_path / ".out.lock"
    lock.write_bytes(b"stale")
    extracted = extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert extracted[0].read_bytes() == b"data"


def test_held_activation_lock_recovers_after_subprocess_crash(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "asset.zip", [("file", b"data")])
    lock = tmp_path / ".out.lock"
    ready = tmp_path / "ready"
    script = (
        "import time; from pathlib import Path; "
        "from csaf.setup.assets import _acquire_activation_lock; "
        f"fd=_acquire_activation_lock(Path({str(lock)!r})); "
        f"Path({str(ready)!r}).write_text('ready'); time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        for _ in range(250):
            if ready.exists() or process.poll() is not None:
                break
            time.sleep(0.02)
        assert ready.exists(), "lock subprocess failed to become ready"
        with pytest.raises(SetupError, match="in progress"):
            extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    finally:
        process.kill()
        process.wait(timeout=10)

    extracted = extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert extracted[0].read_bytes() == b"data"


def test_extract_rechecks_destination_immediately_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip(tmp_path / "asset.zip", [("file", b"data")])

    def race_destination(parent: Path) -> None:
        (parent / "out").mkdir()
        return None

    monkeypatch.setattr(assets_module, "_open_directory_fd", race_destination)
    with pytest.raises(SetupError, match="destination already exists"):
        extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert (tmp_path / "out").is_dir()
    assert not (tmp_path / "out" / "file").exists()


def test_extract_ignores_directory_close_failure_after_successful_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip(tmp_path / "asset.zip", [("file", b"data")])
    monkeypatch.setattr(assets_module, "_fsync_staged_directories", lambda root: None)
    monkeypatch.setattr(assets_module, "_open_directory_fd", lambda parent: 123)
    monkeypatch.setattr(assets_module.os, "fsync", lambda descriptor: None)
    monkeypatch.setattr(
        assets_module.os,
        "close",
        lambda descriptor: (_ for _ in ()).throw(OSError("SECRET close failure")),
    )

    extracted = extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert extracted[0].read_bytes() == b"data"


def test_atomic_file_activation_fsyncs_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / ".state.tmp"
    temporary.write_bytes(b"new")
    target = tmp_path / "state"
    fsynced: list[int] = []
    closed: list[int] = []
    monkeypatch.setattr(assets_module, "_open_directory_fd", lambda parent: 123)
    monkeypatch.setattr(assets_module.os, "fsync", fsynced.append)
    monkeypatch.setattr(assets_module.os, "close", closed.append)
    assets_module._activate_temp_file(temporary, target)
    assert target.read_bytes() == b"new"
    assert fsynced == [123]
    assert closed == [123]


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode contract")
def test_extract_requires_private_csaf_owned_activation_parent(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "asset.zip", [("file", b"data")])
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    with pytest.raises(SetupError, match="CSAF-owned and private"):
        extract_verified_archive(archive, parent / "out", limits=TEST_LIMITS)


def test_extract_rejects_preexisting_symlink_parent(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    archive = _zip(tmp_path / "asset.zip", [("file", b"data")])
    with pytest.raises(SetupError, match="symlink|reparse"):
        extract_verified_archive(archive, link / "out", limits=TEST_LIMITS)
    assert not (real / "out").exists()
