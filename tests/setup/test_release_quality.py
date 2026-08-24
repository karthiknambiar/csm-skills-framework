from __future__ import annotations

import hashlib
import re
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.build_native_release as builder
import scripts.verify_native_install as verifier

VERSION = "0.1.0"
ROOT = Path(__file__).parents[2]


def _identity_wheel(
    path: Path,
    *,
    metadata_headers: bytes = b"Name: fixture\nVersion: 1.2.3\n",
    dist_info: str = "fixture-1.2.3.dist-info",
) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", metadata_headers)
        archive.writestr(f"{dist_info}/WHEEL", b"Wheel-Version: 1.0\nTag: py3-none-any\n")
    return path


def test_concurrent_builder_never_removes_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"wheel")

    def fake_assemble(**kwargs: object) -> Path:
        target = Path(kwargs["output_root"]) / VERSION
        target.mkdir(parents=True)
        (target / "owned").write_text("staging", encoding="utf-8")
        if Path(kwargs["output_root"]) == output:
            raise FileExistsError("interleaved winner")
        return target

    def interleaved_verify(*_: object, **__: object) -> tuple[str, ...]:
        winner = output / VERSION
        winner.mkdir()
        (winner / "winner").write_text("preserve", encoding="utf-8")
        return ()

    monkeypatch.setattr(builder, "_assemble_release", fake_assemble)
    monkeypatch.setattr(builder, "verify_release", interleaved_verify)
    monkeypatch.setattr(
        builder, "_wheel_metadata", lambda _: ("fixture", VERSION, ("py3-none-any",))
    )

    with pytest.raises(builder.ReleaseBuildError, match="already exists|concurrent"):
        builder.build_release(
            repo_root=tmp_path,
            output_root=output,
            wheel=wheel,
            require_clean=False,
        )

    assert (output / VERSION / "winner").read_text(encoding="utf-8") == "preserve"
    assert not list(output.glob(f".{VERSION}.staging-*"))


def test_builder_verifies_staging_before_atomic_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"wheel")
    seen: list[Path] = []

    def fake_assemble(**kwargs: object) -> Path:
        assert Path(kwargs["output_root"]) != output
        target = Path(kwargs["output_root"]) / VERSION
        target.mkdir(parents=True)
        (target / "complete").write_text("yes", encoding="utf-8")
        return target

    def fake_verify(path: Path, **_: object) -> tuple[str, ...]:
        assert not (output / VERSION).exists()
        seen.append(path)
        return ()

    monkeypatch.setattr(builder, "_assemble_release", fake_assemble)
    monkeypatch.setattr(builder, "verify_release", fake_verify)
    monkeypatch.setattr(
        builder, "_wheel_metadata", lambda _: ("fixture", VERSION, ("py3-none-any",))
    )

    result = builder.build_release(
        repo_root=tmp_path,
        output_root=output,
        wheel=wheel,
        require_clean=False,
    )

    assert seen and result == output / VERSION
    assert (result / "complete").read_text(encoding="utf-8") == "yes"


def test_builder_recovers_owned_staging_after_lock_holder_hard_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    script = (
        "import os; from pathlib import Path; import scripts.build_native_release as b; "
        f"out=Path({str(output)!r}); "
        f"fd=b._acquire_build_lock(out / '.{VERSION}.build.lock'); "
        f"b._new_owned_staging(out, {VERSION!r}); os._exit(19)"
    )
    completed = subprocess.run([sys.executable, "-c", script], check=False, timeout=20)
    assert completed.returncode == 19
    assert list(output.glob(f".{VERSION}.staging-*"))
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"wheel")

    def fake_assemble(**kwargs: object) -> Path:
        target = Path(kwargs["output_root"]) / VERSION
        target.mkdir()
        (target / "complete").write_text("yes", encoding="utf-8")
        return target

    monkeypatch.setattr(builder, "_assemble_release", fake_assemble)
    monkeypatch.setattr(builder, "verify_release", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        builder, "_wheel_metadata", lambda _: ("fixture", VERSION, ("py3-none-any",))
    )
    result = builder.build_release(
        repo_root=tmp_path, output_root=output, wheel=wheel, require_clean=False
    )
    assert result == output / VERSION
    assert not list(output.glob(f".{VERSION}.staging-*"))


@pytest.mark.parametrize("residue", ["empty", "zero-marker", "truncated-marker"])
def test_builder_recovers_only_well_defined_precontent_crash_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, residue: str
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    token = "a" * 32
    stale = output / f".{VERSION}.staging-{token}"
    stale.mkdir()
    marker = builder._canonical_json({"schema_version": 1, "version": VERSION, "token": token})
    if residue != "empty":
        content = b"" if residue == "zero-marker" else marker[:17]
        (stale / ".csaf-release-staging.json").write_bytes(content)
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"wheel")

    def fake_assemble(**kwargs: object) -> Path:
        target = Path(kwargs["output_root"]) / VERSION
        target.mkdir()
        (target / "complete").write_text("yes", encoding="utf-8")
        return target

    monkeypatch.setattr(builder, "_assemble_release", fake_assemble)
    monkeypatch.setattr(builder, "verify_release", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        builder, "_wheel_metadata", lambda _: ("fixture", VERSION, ("py3-none-any",))
    )
    assert (
        builder.build_release(
            repo_root=tmp_path, output_root=output, wheel=wheel, require_clean=False
        )
        == output / VERSION
    )
    assert not stale.exists()


@pytest.mark.parametrize("residue", ["unknown-marker", "unknown-entry"])
def test_builder_never_deletes_unknown_nonempty_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, residue: str
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    token = "b" * 32
    stale = output / f".{VERSION}.staging-{token}"
    stale.mkdir()
    if residue == "unknown-marker":
        (stale / ".csaf-release-staging.json").write_bytes(b"not-owner-data")
    else:
        (stale / "unknown").write_bytes(b"content")
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"wheel")
    monkeypatch.setattr(
        builder, "_wheel_metadata", lambda _: ("fixture", VERSION, ("py3-none-any",))
    )
    with pytest.raises(builder.ReleaseBuildError, match="unsafe stale"):
        builder.build_release(
            repo_root=tmp_path, output_root=output, wheel=wheel, require_clean=False
        )
    assert stale.is_dir()


def test_builder_does_not_touch_active_competitor_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    ready = tmp_path / "ready"
    script = (
        "import time; from pathlib import Path; import scripts.build_native_release as b; "
        f"out=Path({str(output)!r}); "
        f"fd=b._acquire_build_lock(out / '.{VERSION}.build.lock'); "
        f"b._new_owned_staging(out, {VERSION!r}); "
        f"Path({str(ready)!r}).write_text('ready'); time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        for _attempt in range(250):
            if ready.exists() or process.poll() is not None:
                break
            time.sleep(0.02)
        assert ready.exists()
        competitor = list(output.glob(f".{VERSION}.staging-*"))
        wheel = tmp_path / "wheel.whl"
        wheel.write_bytes(b"wheel")
        monkeypatch.setattr(
            builder, "_wheel_metadata", lambda _: ("fixture", VERSION, ("py3-none-any",))
        )
        with pytest.raises(builder.ReleaseBuildError, match="concurrent"):
            builder.build_release(
                repo_root=tmp_path, output_root=output, wheel=wheel, require_clean=False
            )
        assert competitor and competitor[0].is_dir()
    finally:
        process.kill()
        process.wait(timeout=10)


def test_builder_pre_publish_fsync_failure_never_exposes_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"wheel")

    def fake_assemble(**kwargs: object) -> Path:
        target = Path(kwargs["output_root"]) / VERSION
        target.mkdir()
        (target / "complete").write_text("yes", encoding="utf-8")
        return target

    monkeypatch.setattr(builder, "_assemble_release", fake_assemble)
    monkeypatch.setattr(builder, "verify_release", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        builder, "_wheel_metadata", lambda _: ("fixture", VERSION, ("py3-none-any",))
    )
    monkeypatch.setattr(
        builder,
        "_fsync_staged_tree",
        lambda _: (_ for _ in ()).throw(OSError("pre-rename fsync failed")),
    )
    with pytest.raises(OSError, match="pre-rename"):
        builder.build_release(
            repo_root=tmp_path, output_root=output, wheel=wheel, require_clean=False
        )
    assert not (output / VERSION).exists()
    assert not list(output.glob(f".{VERSION}.staging-*"))


def test_builder_post_publish_fsync_failure_preserves_published_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"wheel")

    def fake_assemble(**kwargs: object) -> Path:
        target = Path(kwargs["output_root"]) / VERSION
        target.mkdir()
        (target / "complete").write_text("yes", encoding="utf-8")
        return target

    monkeypatch.setattr(builder, "_assemble_release", fake_assemble)
    monkeypatch.setattr(builder, "verify_release", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        builder, "_wheel_metadata", lambda _: ("fixture", VERSION, ("py3-none-any",))
    )
    monkeypatch.setattr(builder, "_fsync_staged_tree", lambda _: None)

    def fail_output_parent_only(path: Path) -> None:
        if path == output:
            raise OSError("post-rename fsync failed")

    monkeypatch.setattr(builder, "_fsync_directory", fail_output_parent_only)
    with pytest.raises(builder.ReleaseDurabilityError, match="durability"):
        builder.build_release(
            repo_root=tmp_path, output_root=output, wheel=wheel, require_clean=False
        )
    assert (output / VERSION / "complete").read_text(encoding="utf-8") == "yes"


def test_verified_asset_rejects_symlink_and_linked_parent(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"verified")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    direct = tmp_path / "direct.bin"
    parent = tmp_path / "linked-parent"
    try:
        direct.symlink_to(target)
        parent.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(verifier.NativeVerificationError, match="asset verification"):
        verifier._verified_asset(direct, sha256=digest, size=target.stat().st_size)
    with pytest.raises(verifier.NativeVerificationError, match="asset verification"):
        verifier._verified_asset(parent / target.name, sha256=digest, size=target.stat().st_size)


def test_windows_reparse_attribute_is_treated_as_link() -> None:
    details = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)

    assert verifier._link_or_reparse(details) is True


def test_builder_cli_error_is_stable_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel = tmp_path / "fixture.whl"
    wheel.write_bytes(b"wheel")
    monkeypatch.setattr(
        builder,
        "build_release",
        lambda **_: (_ for _ in ()).throw(
            builder.ReleaseBuildError(
                "C:\\secret\\" + "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789" + "\x1b[31m"
            )
        ),
    )

    assert builder.main(["--wheel", str(wheel), "--allow-dirty"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "release build failed\n"


def test_release_toolchain_and_actions_are_immutable_and_hash_locked() -> None:
    workflows = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in (".github/workflows/ci.yml", ".github/workflows/release.yml")
    )
    uses = [line.split("uses:", 1)[1].strip() for line in workflows.splitlines() if "uses:" in line]

    assert uses and all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}(?:\s+#.+)?", item) for item in uses)
    assert "pip install build" not in workflows
    assert "pip install cryptography" not in workflows
    assert "--require-hashes" in workflows
    assert "--no-isolation" in workflows
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires = ["hatchling==1.27.0"]' in pyproject
    lock = (ROOT / "requirements/release-tools.txt").read_text(encoding="utf-8")
    assert "build==" in lock and "hatchling==" in lock and "cryptography==" in lock
    assert "--hash=sha256:" in lock


@pytest.mark.parametrize(
    ("headers", "dist_info"),
    [
        (b"Name: fixture\nName: other\nVersion: 1.2.3\n", "fixture-1.2.3.dist-info"),
        (b"Name: fixture\nVersion: 1.2.3\nVersion: 9.9.9\n", "fixture-1.2.3.dist-info"),
        (b"Name: fixture\nVersion: 1.2.3\n", "other-1.2.3.dist-info"),
        (b"Name: fixture\nVersion: 1.2.3\n", "fixture-9.9.9.dist-info"),
    ],
)
def test_wheel_identity_requires_unique_headers_and_dist_info_agreement(
    tmp_path: Path, headers: bytes, dist_info: str
) -> None:
    wheel = _identity_wheel(
        tmp_path / "fixture-1.2.3-py3-none-any.whl", metadata_headers=headers, dist_info=dist_info
    )

    with pytest.raises(builder.ReleaseBuildError, match="metadata"):
        builder._wheel_metadata(wheel)
