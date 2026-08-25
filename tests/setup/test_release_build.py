"""Contracts for deterministic native release assembly."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

import pytest
import scripts.build_native_release as native_builder
import scripts.verify_native_install as native_verifier
from scripts.build_native_release import (
    FIXED_ZIP_TIMESTAMP,
    PLATFORM_IDS,
    ReleaseBuildError,
    _download,
    _validate_wheel,
    build_release,
)
from scripts.verify_native_install import NativeVerificationError, _extract_uv, _verified_asset

VERSION = "0.1.0"

EXPECTED_RUNTIME_DEPENDENCIES = [
    "fastapi==0.141.1",
    "starlette==1.6.0",
    "pydantic==2.13.4",
    "pydantic-core==2.46.4",
    "annotated-types==0.8.0",
    "annotated-doc==0.0.5",
    "typing-extensions==4.16.0",
    "typing-inspection==0.4.4",
    "typer==0.27.1",
    "click==8.4.2",
    "colorama==0.4.6",
    "shellingham==1.5.4",
    "rich==15.0.0",
    "markdown-it-py==4.2.0",
    "mdurl==0.1.2",
    "pygments==2.21.0",
    "uvicorn==0.52.4",
    "h11==0.16.0",
    "anyio==4.14.2",
    "idna==3.19",
]

EXPECTED_PLATFORM_TAGS = {
    "linux-arm64": "manylinux_2_17_aarch64",
    "linux-x64": "manylinux_2_17_x86_64",
    "macos-arm64": "macosx_11_0_arm64",
    "macos-x64": "macosx_10_12_x86_64",
    "windows-arm64": "win_arm64",
    "windows-x64": "win_amd64",
}


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _wheel(
    path: Path,
    name: str,
    version: str,
    members: dict[str, bytes] | None = None,
    *,
    tags: tuple[str, ...] = ("py3-none-any",),
) -> Path:
    distribution = name.replace("-", "_")
    metadata = f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n\n".encode()
    wheel_metadata = (
        "Wheel-Version: 1.0\nGenerator: csaf-test\nRoot-Is-Purelib: true\n"
        + "".join(f"Tag: {tag}\n" for tag in tags)
        + "\n"
    ).encode()
    payloads = {
        f"{distribution}-{version}.dist-info/METADATA": metadata,
        f"{distribution}-{version}.dist-info/WHEEL": wheel_metadata,
        **(members or {}),
    }
    with zipfile.ZipFile(path, "w") as archive:
        for member, payload in sorted(payloads.items()):
            info = zipfile.ZipInfo(member, FIXED_ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload)
    return path


def _write_fixture_repo(root: Path) -> tuple[Path, Path]:
    (root / "plugins/csaf/.claude-plugin").mkdir(parents=True)
    skill = root / "plugins/csaf/skills/csaf"
    (skill / "references").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / "installer").mkdir()
    (root / "src/csaf/templates/qbr").mkdir(parents=True)

    (skill / "SKILL.md").write_bytes(b"---\r\nname: csaf\r\n---\r\n")
    (skill / "references/qbr.md").write_bytes(b"QBR\r\n")
    plugin = {"name": "csaf", "version": VERSION, "description": "fixture"}
    (root / "plugins/csaf/.claude-plugin/plugin.json").write_bytes(_canonical_json(plugin))
    marketplace = {
        "name": "csaf",
        "owner": {"name": "CSAF"},
        "plugins": [{"name": "csaf", "source": "./plugins/csaf", "version": VERSION}],
    }
    (root / ".claude-plugin/marketplace.json").write_bytes(_canonical_json(marketplace))
    dependencies = {
        "schema_version": 1,
        "officecli": {
            "version": "1.0.143",
            "minimum_version": "1.0.137",
            "assets": {
                platform: {
                    "url": f"https://example.invalid/{platform}/officecli",
                    "sha256": "a" * 64,
                    "size": 123,
                }
                for platform in PLATFORM_IDS
            },
        },
    }
    (root / "installer/dependencies.json").write_bytes(_canonical_json(dependencies))
    (root / "installer/install.sh").write_bytes(b"#!/bin/sh\r\necho install\r\n")
    (root / "installer/install.ps1").write_bytes(b"Write-Output install\r\n")
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "csaf"\n'
        f'version = "{VERSION}"\n'
        "[tool.csaf.native-runtime]\n"
        'python-version = "3.12"\n'
        'implementation = "cp"\n'
        'abi = "cp312"\n'
        'dependencies = ["fixture-dependency==1.2.3"]\n'
        "[tool.csaf.native-runtime.platform-tags]\n"
        'linux-arm64 = "manylinux_2_17_aarch64"\n'
        'linux-x64 = "manylinux_2_17_x86_64"\n'
        'macos-arm64 = "macosx_11_0_arm64"\n'
        'macos-x64 = "macosx_10_12_x86_64"\n'
        'windows-arm64 = "win_arm64"\n'
        'windows-x64 = "win_amd64"\n',
        encoding="utf-8",
    )

    templates = {
        "default-qbr.pptx": b"pptx-template-bytes",
        "default-qbr.docx": b"docx-template-bytes",
        "provenance.json": _canonical_json({"license": "Apache-2.0"}),
    }
    for name, payload in templates.items():
        (root / "src/csaf/templates/qbr" / name).write_bytes(payload)

    wheel = _wheel(
        root / f"csaf-{VERSION}-py3-none-any.whl",
        "csaf",
        VERSION,
        {f"csaf/templates/qbr/{name}": payload for name, payload in templates.items()},
    )
    cache = root / "wheelhouse"
    for platform in PLATFORM_IDS:
        platform_cache = cache / platform
        platform_cache.mkdir(parents=True)
        _wheel(
            platform_cache / "fixture_dependency-1.2.3-py3-none-any.whl",
            "fixture-dependency",
            "1.2.3",
        )
    fixture_wheel = cache / PLATFORM_IDS[0] / "fixture_dependency-1.2.3-py3-none-any.whl"
    wheel_hash = hashlib.sha256(fixture_wheel.read_bytes()).hexdigest()
    with (root / "pyproject.toml").open("a", encoding="utf-8", newline="\n") as project:
        project.write(
            "[tool.csaf.native-release.wheels.common]\n"
            f'"{fixture_wheel.name}" = "{wheel_hash}"\n'
            "[tool.csaf.native-release.wheels.platform]\n"
            + "".join(f'"{platform}" = {{}}\n' for platform in PLATFORM_IDS)
        )
    return wheel, cache


def _build(repo: Path, output: Path, *, clean: bool = False) -> Path:
    wheel, cache = _write_fixture_repo(repo)
    return build_release(
        repo_root=repo,
        output_root=output,
        wheel=wheel,
        wheelhouse_root=cache,
        requested_version=VERSION,
        require_clean=clean,
    )


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_project_pins_the_complete_cpython_312_native_runtime() -> None:
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text("utf-8"))
    native = project["tool"]["csaf"]["native-runtime"]

    assert native["python-version"] == "3.12"
    assert native["implementation"] == "cp"
    assert native["abi"] == "cp312"
    assert native["dependencies"] == EXPECTED_RUNTIME_DEPENDENCIES
    assert native["platform-tags"] == EXPECTED_PLATFORM_TAGS
    wheels = project["tool"]["csaf"]["native-release"]["wheels"]
    assert len(wheels["common"]) == 19
    assert set(wheels["platform"]) == set(PLATFORM_IDS)
    assert all(len(wheels["platform"][platform]) == 1 for platform in PLATFORM_IDS)
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in wheels["common"].values())
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", digest)
        for platform in PLATFORM_IDS
        for digest in wheels["platform"][platform].values()
    )


def test_build_is_deterministic_canonical_and_complete(tmp_path: Path) -> None:
    first = _build(tmp_path / "first-repo", tmp_path / "first-out")
    second = _build(tmp_path / "second-repo", tmp_path / "second-out")

    assert first.name == VERSION
    assert _file_hashes(first) == _file_hashes(second)
    manifest_path = first / "csaf-release-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest_path.read_bytes() == _canonical_json(manifest)
    assert manifest["version"] == VERSION
    assert list(manifest["runtime"]) == list(PLATFORM_IDS)
    assert set(manifest) == {
        "schema_version",
        "version",
        "runtime",
        "codex_skill",
        "claude_plugin",
        "officecli",
    }

    expected_names = {
        "SHA256SUMS",
        "csaf-release-manifest.json",
        f"csaf-codex-skill-{VERSION}.zip",
        f"csaf-claude-plugin-{VERSION}.zip",
        "install.ps1",
        "install.sh",
        *(f"csaf-runtime-{platform}-{VERSION}.zip" for platform in PLATFORM_IDS),
    }
    assert {path.name for path in first.iterdir()} == expected_names

    for archive_name in (
        f"csaf-codex-skill-{VERSION}.zip",
        f"csaf-claude-plugin-{VERSION}.zip",
    ):
        with zipfile.ZipFile(first / archive_name) as archive:
            assert archive.namelist() == sorted(archive.namelist())
            assert all(info.date_time == FIXED_ZIP_TIMESTAMP for info in archive.infolist())
            assert all(not info.is_dir() for info in archive.infolist())
            assert archive.read("csaf/SKILL.md") == b"---\nname: csaf\n---\n"

    with zipfile.ZipFile(first / f"csaf-claude-plugin-{VERSION}.zip") as archive:
        assert ".claude-plugin/plugin.json" in archive.namelist()

    template_names = {
        "csaf/templates/qbr/default-qbr.pptx",
        "csaf/templates/qbr/default-qbr.docx",
        "csaf/templates/qbr/provenance.json",
    }
    for platform in PLATFORM_IDS:
        runtime_name = f"csaf-runtime-{platform}-{VERSION}.zip"
        runtime_path = first / runtime_name
        asset = manifest["runtime"][platform]
        assert asset["size"] == runtime_path.stat().st_size
        assert asset["sha256"] == hashlib.sha256(runtime_path.read_bytes()).hexdigest()
        with zipfile.ZipFile(runtime_path) as archive:
            assert archive.namelist() == sorted(archive.namelist())
            assert set(archive.namelist()) == {
                "runtime-bundle.json",
                f"csaf-{VERSION}-py3-none-any.whl",
                "requirements.lock",
                "wheelhouse/fixture_dependency-1.2.3-py3-none-any.whl",
            }
            runtime = json.loads(archive.read("runtime-bundle.json"))
            assert archive.read("runtime-bundle.json") == _canonical_json(runtime)
            assert runtime["version"] == VERSION
            assert runtime["platform"] == platform
            for member, record in runtime["files"].items():
                payload = archive.read(member)
                assert record == {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }
            with zipfile.ZipFile(archive.open(f"csaf-{VERSION}-py3-none-any.whl")) as wheel_archive:
                assert template_names <= set(wheel_archive.namelist())

    sums = (first / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert sums == sorted(sums, key=lambda line: line.split("  ", 1)[1])
    assert {line.split("  ", 1)[1] for line in sums} == expected_names - {"SHA256SUMS"}


@pytest.mark.parametrize("failure", ["package", "plugin", "marketplace", "wheelhouse"])
def test_build_rejects_mismatched_or_missing_inputs(tmp_path: Path, failure: str) -> None:
    repo = tmp_path / "repo"
    wheel, cache = _write_fixture_repo(repo)
    if failure == "package":
        (repo / "pyproject.toml").write_text(
            (repo / "pyproject.toml").read_text().replace(VERSION, "0.2.0"),
            encoding="utf-8",
        )
    elif failure == "plugin":
        path = repo / "plugins/csaf/.claude-plugin/plugin.json"
        path.write_text(path.read_text().replace(VERSION, "0.2.0"), encoding="utf-8")
    elif failure == "marketplace":
        path = repo / ".claude-plugin/marketplace.json"
        path.write_text(path.read_text().replace(VERSION, "0.2.0"), encoding="utf-8")
    else:
        (cache / PLATFORM_IDS[0] / "fixture_dependency-1.2.3-py3-none-any.whl").unlink()

    with pytest.raises(ReleaseBuildError, match="version|wheelhouse"):
        build_release(
            repo_root=repo,
            output_root=tmp_path / "out",
            wheel=wheel,
            wheelhouse_root=cache,
            requested_version=VERSION,
            require_clean=False,
        )
    assert not (tmp_path / "out" / VERSION).exists()


def test_build_rejects_hash_mismatched_cache_wheel_with_matching_metadata(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    wheel, cache = _write_fixture_repo(repo)
    malicious = cache / "windows-x64/fixture_dependency-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(malicious, "a") as archive:
        archive.writestr("fixture_dependency/backdoor.py", b"matching metadata, different bytes")

    with pytest.raises(ReleaseBuildError, match="hash|identity"):
        build_release(
            repo_root=repo,
            output_root=tmp_path / "out",
            wheel=wheel,
            wheelhouse_root=cache,
            requested_version=VERSION,
            require_clean=False,
        )


@pytest.mark.parametrize("mutation", ["tag", "member"])
def test_build_rejects_unsafe_or_mistagged_csaf_wheel(tmp_path: Path, mutation: str) -> None:
    repo = tmp_path / "repo"
    wheel, cache = _write_fixture_repo(repo)
    if mutation == "tag":
        _wheel(wheel, "csaf", VERSION, tags=("cp312-cp312-win_amd64",))
    else:
        _wheel(wheel, "csaf", VERSION, {"../escaped.py": b"hostile"})

    with pytest.raises(ReleaseBuildError, match="CSAF wheel|member|tag"):
        build_release(
            repo_root=repo,
            output_root=tmp_path / "out",
            wheel=wheel,
            wheelhouse_root=cache,
            requested_version=VERSION,
            require_clean=False,
        )


def test_build_rejects_dirty_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wheel, cache = _write_fixture_repo(repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CSAF",
            "-c",
            "user.email=csaf@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )
    (repo / "dirty.txt").write_text("dirty", encoding="utf-8")

    with pytest.raises(ReleaseBuildError, match="dirty"):
        build_release(
            repo_root=repo,
            output_root=tmp_path / "out",
            wheel=wheel,
            wheelhouse_root=cache,
            requested_version=VERSION,
            require_clean=True,
        )


def test_ci_runs_cross_platform_offline_native_smoke() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text("utf-8")

    for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert runner in source
    assert "python -m pip --version" in source
    assert "CSAF_INSTALLER_NETWORK_FORBIDDEN" in source
    assert "--offline" in source
    assert "setup doctor --json" in source
    assert "matrix.smoke_csaf" in source
    assert "check_secrets.py --worktree --tracked --history" in source
    assert "install.sh --dry-run" in source
    assert "install.ps1 -WhatIf" in source
    assert "--verify-release" in source
    assert "check_secrets.py" in source and "--package" in source


def test_ci_invokes_pytest_as_a_python_module() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text("utf-8")

    assert "- run: python -m pytest" in source
    assert "- run: pytest" not in source


def _native_smoke_job(source: str) -> str:
    marker = "  native-offline-smoke:\n"
    assert source.count(marker) == 1
    job = source.split(marker, 1)[1]
    following_job = re.search(r"(?m)^  [a-zA-Z0-9_-]+:\s*$", job)
    return job[: following_job.start()] if following_job else job


def _native_smoke_matrix(source: str) -> tuple[tuple[str, ...], ...]:
    job = _native_smoke_job(source)
    include = job.split("        include:\n", 1)[1].split("    env:\n", 1)[0]
    rows: list[dict[str, str]] = []
    for line in include.splitlines():
        if line.startswith("          - "):
            rows.append({})
            field = line.removeprefix("          - ")
        elif line.startswith("            "):
            assert rows
            field = line.removeprefix("            ")
        elif not line.strip():
            continue
        else:
            raise AssertionError(f"unexpected native-smoke matrix line: {line!r}")
        key, separator, value = field.partition(": ")
        assert separator and key not in rows[-1]
        rows[-1][key] = value

    keys = (
        "runner",
        "platform",
        "smoke-python",
        "smoke_csaf",
        "verifier-python",
        "verifier-csaf",
    )
    assert all(tuple(row) == keys for row in rows)
    return tuple(tuple(row[key] for key in keys) for row in rows)


def _native_smoke_step_command(source: str, name: str) -> str:
    job = _native_smoke_job(source)
    marker = f"      - name: {name}\n"
    assert job.count(marker) == 1
    step = job.split(marker, 1)[1].split("\n      - ", 1)[0]
    run_line = re.search(r"(?m)^        run: (?P<value>.*)$", step)
    assert run_line
    value = run_line.group("value")
    if value not in {"|-", ">-"}:
        return value
    block = step[run_line.end() :]
    return " ".join(line.strip() for line in block.splitlines() if line.strip())


def _assert_native_smoke_workflow(source: str) -> None:
    assert _native_smoke_matrix(source) == (
        (
            "ubuntu-latest",
            "linux-x64",
            "../.native-smoke/bin/python",
            "../.native-smoke/bin/csaf",
            ".native-smoke/bin/python",
            ".native-smoke/bin/csaf",
        ),
        (
            "macos-latest",
            "macos-arm64",
            "../.native-smoke/bin/python",
            "../.native-smoke/bin/csaf",
            ".native-smoke/bin/python",
            ".native-smoke/bin/csaf",
        ),
        (
            "windows-latest",
            "windows-x64",
            "../.native-smoke/Scripts/python.exe",
            "../.native-smoke/Scripts/csaf.exe",
            ".native-smoke/Scripts/python.exe",
            ".native-smoke/Scripts/csaf.exe",
        ),
    )
    assert _native_smoke_step_command(
        source, "Install from the bundle with no index (--offline)"
    ) == (
        "${{ matrix.smoke-python }} -m pip install --no-index --no-deps "
        "--require-hashes --find-links wheelhouse -r requirements.lock"
    )
    assert _native_smoke_step_command(source, "Install trusted TLS verifier dependency") == (
        "${{ matrix.verifier-python }} -m pip install --require-hashes "
        "-r requirements/release-tools.txt"
    )
    assert _native_smoke_step_command(
        source, "Run consented install and require READY doctor with external network blocked"
    ) == (
        "${{ matrix.verifier-python }} scripts/verify_native_install.py "
        "--release-dir dist/native-ci/0.1.0 "
        "--dependencies installer/dependencies.json "
        "--platform ${{ matrix.platform }} "
        "--uv .native-assets/uv-archive "
        "--officecli .native-assets/officecli "
        "--csaf ${{ matrix.verifier-csaf }}"
    )


def test_ci_runs_native_verifier_with_the_smoke_interpreter() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text("utf-8")

    _assert_native_smoke_workflow(source)


def test_native_smoke_contract_rejects_one_bad_matrix_row() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text("utf-8")
    mutated = source.replace(
        "- runner: macos-latest\n"
        "            platform: macos-arm64\n"
        "            smoke-python: ../.native-smoke/bin/python\n"
        "            smoke_csaf: ../.native-smoke/bin/csaf\n"
        "            verifier-python: .native-smoke/bin/python",
        "- runner: macos-latest\n"
        "            platform: macos-arm64\n"
        "            smoke-python: ../.native-smoke/bin/python\n"
        "            smoke_csaf: ../.native-smoke/bin/csaf\n"
        "            verifier-python: .native-smoke/broken/python",
    )
    assert mutated != source

    with pytest.raises(AssertionError):
        _assert_native_smoke_workflow(mutated)


def test_ci_pairs_macos_latest_with_the_arm64_bundle() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text("utf-8")

    assert "- runner: macos-latest\n            platform: macos-arm64" in source
    assert "platform: macos-x64" not in source


def _release_native_install_job(source: str) -> str:
    marker = "  verify-native-install:\n"
    assert source.count(marker) == 1
    job = source.split(marker, 1)[1]
    following_job = re.search(r"(?m)^  [a-zA-Z0-9_-]+:\s*$", job)
    return job[: following_job.start()] if following_job else job


def _release_native_install_matrix(source: str) -> tuple[tuple[str, ...], ...]:
    job = _release_native_install_job(source)
    include = job.split("        include:\n", 1)[1].split("    steps:\n", 1)[0]
    rows: list[dict[str, str]] = []
    for line in include.splitlines():
        if line.startswith("          - "):
            rows.append({})
            field = line.removeprefix("          - ")
        elif line.startswith("            "):
            assert rows
            field = line.removeprefix("            ")
        elif not line.strip():
            continue
        else:
            raise AssertionError(f"unexpected release install matrix line: {line!r}")
        key, separator, value = field.partition(": ")
        assert separator and key not in rows[-1]
        rows[-1][key] = value

    keys = ("runner", "platform", "smoke-python", "smoke-csaf", "verifier-python")
    assert all(tuple(row) == keys for row in rows)
    return tuple(tuple(row[key] for key in keys) for row in rows)


def _release_native_install_step_command(source: str, name: str) -> str:
    job = _release_native_install_job(source)
    marker = f"      - name: {name}\n"
    assert job.count(marker) == 1
    step = job.split(marker, 1)[1].split("\n      - ", 1)[0]
    run_line = re.search(r"(?m)^        run: (?P<value>.*)$", step)
    assert run_line
    value = run_line.group("value")
    if value not in {"|-", ">-"}:
        return value
    block = step[run_line.end() :]
    return " ".join(line.strip() for line in block.splitlines() if line.strip())


def _assert_release_native_install_workflow(source: str) -> None:
    assert _release_native_install_matrix(source) == (
        (
            "ubuntu-latest",
            "linux-x64",
            "../.native-smoke/bin/python",
            ".native-smoke/bin/csaf",
            ".native-smoke/bin/python",
        ),
        (
            "macos-latest",
            "macos-arm64",
            "../.native-smoke/bin/python",
            ".native-smoke/bin/csaf",
            ".native-smoke/bin/python",
        ),
        (
            "windows-latest",
            "windows-x64",
            "../.native-smoke/Scripts/python.exe",
            ".native-smoke/Scripts/csaf.exe",
            ".native-smoke/Scripts/python.exe",
        ),
    )
    assert _release_native_install_step_command(
        source, "Install the private console from the release bundle only"
    ) == (
        "${{ matrix.smoke-python }} -m pip install --no-index --no-deps "
        "--require-hashes --find-links wheelhouse -r requirements.lock"
    )
    assert _release_native_install_step_command(
        source, "Pre-stage verifier TLS dependency before network isolation"
    ) == (
        "${{ matrix.verifier-python }} -m pip install --require-hashes "
        "-r requirements/release-tools.txt"
    )
    assert _release_native_install_step_command(
        source, "Require READY activated launcher with supplemental application egress guard"
    ) == (
        "${{ matrix.verifier-python }} scripts/verify_native_install.py "
        "--release-dir release "
        "--dependencies installer/dependencies.json "
        "--platform ${{ matrix.platform }} "
        "--uv .native-assets/uv-archive "
        "--officecli .native-assets/officecli "
        "--csaf ${{ matrix.smoke-csaf }}"
    )


def test_release_native_install_matrix_and_commands_are_exact() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/release.yml").read_text("utf-8")

    _assert_release_native_install_workflow(source)


def test_release_native_install_contract_rejects_one_bad_matrix_row() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/release.yml").read_text("utf-8")
    mutated = source.replace(
        "- runner: windows-latest\n"
        "            platform: windows-x64\n"
        "            smoke-python: ../.native-smoke/Scripts/python.exe",
        "- runner: windows-latest\n"
        "            platform: windows-x64\n"
        "            smoke-python: ../.native-smoke/Scripts/broken.exe",
    )
    assert mutated != source

    with pytest.raises(AssertionError):
        _assert_release_native_install_workflow(mutated)


def _assert_prerelease_publication(source: str) -> None:
    publish_header = "  publish:\n"
    publish_start = source.index(publish_header)
    publish_tail = source[publish_start + len(publish_header) :]
    next_job_or_top_level = re.search(r"(?m)^(?:\S|  [A-Za-z0-9_-]+:)", publish_tail)
    publish_end = (
        publish_start + len(publish_header) + next_job_or_top_level.start()
        if next_job_or_top_level
        else len(source)
    )
    publish_job = source[publish_start:publish_end]
    publication = (
        "      - uses: softprops/action-gh-release@"
        "6da8fa9354ddfdc4aeace5fc48d7f679b5214090 # v2.4.1\n"
        "        with:\n"
        "          name: CSAF v0.1.0 (Testing Prerelease)\n"
        "          prerelease: true\n"
        "          make_latest: false\n"
        "          body: |\n"
        "            This is a testing prerelease and is not intended for production use. "
        "The native agent integration is still being tested.\n"
        "\n"
        "            Review the installation and consent plan before proceeding. CSAF "
        "proposes integration with Codex, Claude, and Gemini for all detected targets. "
        "Installation of the pinned OfficeCLI 1.0.143 is mandatory and requires your consent.\n"
        "\n"
        "            Direct versioned v0.1.0 installer URLs remain available for Windows, "
        "macOS, and Linux.\n"
        "          files: release/*\n"
        "          fail_on_unmatched_files: true\n"
    )
    assert source.lower().count("softprops/action-gh-release@") == 1
    assert publication in publish_job


def test_prerelease_publication_requires_mandatory_pinned_officecli_with_consent() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/release.yml").read_text("utf-8")
    mutated = source.replace(
        "pinned OfficeCLI 1.0.143 is mandatory and requires your consent",
        "pinned OfficeCLI 1.0.143 is optional and requires no consent",
        1,
    )
    assert mutated != source

    with pytest.raises(AssertionError):
        _assert_prerelease_publication(mutated)


def test_prerelease_publication_cannot_be_satisfied_by_commented_metadata() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/release.yml").read_text("utf-8")
    mutated = source.replace(
        "          name: CSAF v0.1.0 (Testing Prerelease)\n"
        "          prerelease: true\n"
        "          make_latest: false\n",
        "          # name: CSAF v0.1.0 (Testing Prerelease)\n"
        "          # prerelease: true\n"
        "          # make_latest: false\n",
        1,
    )
    assert mutated != source

    with pytest.raises(AssertionError):
        _assert_prerelease_publication(mutated)


def test_prerelease_publication_requires_release_action_in_publish_job() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/release.yml").read_text("utf-8")
    action_start = source.index("      - uses: softprops/action-gh-release@")
    action_end = source.index("          fail_on_unmatched_files: true\n", action_start) + len(
        "          fail_on_unmatched_files: true\n"
    )
    action = source[action_start:action_end]
    without_publish_action = source[:action_start] + source[action_end:]
    mutated = without_publish_action.replace("    steps:\n", "    steps:\n" + action, 1)
    assert mutated != source

    with pytest.raises(AssertionError):
        _assert_prerelease_publication(mutated)


def test_prerelease_publication_rejects_top_level_block_scalar_decoy() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/release.yml").read_text("utf-8")
    action_start = source.index("      - uses: softprops/action-gh-release@")
    action_end = source.index("          fail_on_unmatched_files: true\n", action_start) + len(
        "          fail_on_unmatched_files: true\n"
    )
    action = source[action_start:action_end]
    without_publish_action = source[:action_start] + source[action_end:]
    mutated = "env:\n  PRERELEASE_DECOY: |\n" + action + without_publish_action
    assert mutated != source

    with pytest.raises(AssertionError):
        _assert_prerelease_publication(mutated)


def test_prerelease_publication_rejects_second_release_action() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/release.yml").read_text("utf-8")
    unsafe_release = (
        "      - uses: softprops/action-gh-release@"
        "6da8fa9354ddfdc4aeace5fc48d7f679b5214090\n"
        "        with:\n"
        "          prerelease: false\n"
        "          files: release/*\n"
    )
    final_safe_setting = "          fail_on_unmatched_files: true\n"
    mutated = source.replace(final_safe_setting, final_safe_setting + unsafe_release, 1)
    assert mutated != source

    with pytest.raises(AssertionError):
        _assert_prerelease_publication(mutated)


@pytest.mark.parametrize(
    "action_reference",
    [
        "'softprops/action-gh-release@6da8fa9354ddfdc4aeace5fc48d7f679b5214090'",
        "SOFTPROPS/ACTION-GH-RELEASE@6da8fa9354ddfdc4aeace5fc48d7f679b5214090",
    ],
)
def test_prerelease_publication_rejects_disguised_second_release_action(
    action_reference: str,
) -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/release.yml").read_text("utf-8")
    unsafe_release = (
        f"      - uses: {action_reference}\n"
        "        with:\n"
        "          prerelease: false\n"
        "          files: release/*\n"
    )
    final_safe_setting = "          fail_on_unmatched_files: true\n"
    mutated = source.replace(final_safe_setting, final_safe_setting + unsafe_release, 1)
    assert mutated != source

    with pytest.raises(AssertionError):
        _assert_prerelease_publication(mutated)


def test_release_workflow_is_tag_gated_matrixed_and_scanned_before_upload() -> None:
    source = (Path(__file__).parents[2] / ".github/workflows/release.yml").read_text("utf-8")

    assert 'tags: ["v*"]' in source
    assert "contents: write" in source
    assert "scripts/build_native_release.py" in source
    assert "SHA256SUMS" in source
    assert "check_secrets.py" in source
    assert "softprops/action-gh-release" in source
    _assert_prerelease_publication(source)
    assert "verify-native-install:" in source
    assert "--verify-release release --platform" in source
    assert "check_secrets.py" in source and "--package" in source
    assert '"runtime-bundle.json"' in (
        Path(__file__).parents[2] / "scripts/build_native_release.py"
    ).read_text("utf-8")
    assert "needs: [assemble, validate-platform, verify-native-install]" in source
    assert "sudo unshare --net --mount-proc" in source
    assert "ip link set lo up" in source
    assert "matrix.verifier-python" in source
    offline_install = source.index("--no-index --no-deps --require-hashes")
    tls_dependency = source.index("matrix.verifier-python }} -m pip install --require-hashes")
    namespace = source.index("sudo unshare --net --mount-proc")
    assert offline_install < tls_dependency < namespace
    assert namespace < source.index("scripts/verify_native_install.py", namespace)
    assert 'cp "dist/native/${GITHUB_REF_NAME#v}/"* release-assets/' in source
    for platform in PLATFORM_IDS:
        assert platform in source
    assert source.index("check_secrets.py") < source.index("softprops/action-gh-release")


def test_deep_release_verifier_rejects_corrupt_inner_bundle_with_valid_outer_hashes(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    release = _build(repo, tmp_path / "out")
    platform = "windows-x64"
    assert native_builder.verify_release(release, repo_root=repo, platform=platform) == (platform,)

    archive_path = release / f"csaf-runtime-{platform}-{VERSION}.zip"
    with zipfile.ZipFile(archive_path) as opened:
        members = {name: opened.read(name) for name in opened.namelist()}
    dependency = "wheelhouse/fixture_dependency-1.2.3-py3-none-any.whl"
    members[dependency] += b"corruption"
    native_builder._write_zip(archive_path, members)
    manifest_path = release / "csaf-release-manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["runtime"][platform]["sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest["runtime"][platform]["size"] = archive_path.stat().st_size
    manifest_path.write_bytes(_canonical_json(manifest))
    sums_path = release / "SHA256SUMS"
    sums_path.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(release.iterdir())
            if path.is_file() and path != sums_path
        ),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ReleaseBuildError, match="runtime|wheel|hash"):
        native_builder.verify_release(release, repo_root=repo, platform=platform)


def test_deep_release_verifier_rejects_rehashed_modified_installer(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    release = _build(repo, tmp_path / "out")
    installer = release / "install.sh"
    installer.write_bytes(installer.read_bytes() + b"# modified\n")
    sums_path = release / "SHA256SUMS"
    sums_path.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(release.iterdir())
            if path.is_file() and path != sums_path
        ),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ReleaseBuildError, match="installer artifact mismatch"):
        native_builder.verify_release(release, repo_root=repo)


@pytest.mark.parametrize(
    ("limit", "value", "message"),
    [
        ("MAX_RELEASE_ARCHIVE_BYTES", 1, "archive size limit"),
        ("MAX_RELEASE_ARCHIVE_MEMBERS", 0, "archive member limit"),
        ("MAX_RELEASE_MEMBER_BYTES", 4, "archive member size limit"),
        ("MAX_RELEASE_TOTAL_BYTES", 4, "archive expansion limit"),
        ("MAX_RELEASE_COMPRESSION_RATIO", 1, "archive compression ratio"),
    ],
)
def test_deep_verifier_enforces_archive_resource_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    value: int,
    message: str,
) -> None:
    archive = tmp_path / "bounded.zip"
    native_builder._write_zip(archive, {"payload.txt": b"A" * 1_000})
    monkeypatch.setattr(native_builder, limit, value, raising=False)

    with pytest.raises(ReleaseBuildError, match=message):
        native_builder._archive_payloads(archive)


def test_deep_verifier_streams_outer_archive_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    release = _build(repo, tmp_path / "out")
    runtime = release / f"csaf-runtime-windows-x64-{VERSION}.zip"
    original_read_bytes = Path.read_bytes

    def reject_archive_materialization(path: Path) -> bytes:
        if path == runtime:
            raise AssertionError("outer release archives must be hashed incrementally")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_archive_materialization)

    assert native_builder.verify_release(release, repo_root=repo, platform="windows-x64") == (
        "windows-x64",
    )


def test_deep_verifier_bounds_nested_archive_member_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    release = _build(repo, tmp_path / "out")

    def reject_unbounded_read(*_: object, **__: object) -> bytes:
        raise AssertionError("nested archive members must use explicitly bounded reads")

    monkeypatch.setattr(zipfile.ZipFile, "read", reject_unbounded_read)

    assert native_builder.verify_release(release, repo_root=repo, platform="windows-x64") == (
        "windows-x64",
    )


@pytest.mark.parametrize(
    "member",
    [
        "CON.txt",
        "folder/NUL.json",
        "stream.txt:payload",
        "trailing-dot.",
        "trailing-space ",
        "raw/./dot.txt",
        "control-\x01.txt",
    ],
)
def test_release_archive_names_are_safe_for_generation_and_verification(
    tmp_path: Path, member: str
) -> None:
    generated = tmp_path / "generated.zip"
    with pytest.raises(ReleaseBuildError, match="unsafe archive member"):
        native_builder._write_zip(generated, {member: b"clean"})

    hostile = tmp_path / "hostile.zip"
    with zipfile.ZipFile(hostile, "w") as archive:
        archive.writestr(member, b"clean")
    with pytest.raises(ReleaseBuildError, match="unsafe archive member"):
        native_builder._archive_payloads(hostile)


@pytest.mark.parametrize(
    "members",
    [
        {"Folder/File.txt": b"one", "folder/file.TXT": b"two"},
        {"caf\u00e9.txt": b"one", "cafe\u0301.txt": b"two"},
    ],
)
def test_release_archive_names_reject_normalized_collisions_during_generation_and_verification(
    tmp_path: Path, members: dict[str, bytes]
) -> None:
    generated = tmp_path / "generated.zip"
    with pytest.raises(ReleaseBuildError, match="unsafe archive member"):
        native_builder._write_zip(generated, members)

    hostile = tmp_path / "hostile.zip"
    with zipfile.ZipFile(hostile, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    with pytest.raises(ReleaseBuildError, match="unsafe archive member"):
        native_builder._archive_payloads(hostile)


def test_wheel_validation_rejects_embedded_tag_mismatch(tmp_path: Path) -> None:
    wheel = _wheel(
        tmp_path / "fixture_dependency-1.2.3-cp312-cp312-win_amd64.whl",
        "fixture-dependency",
        "1.2.3",
        tags=("cp312-cp312-manylinux_2_17_x86_64",),
    )

    with pytest.raises(ReleaseBuildError, match="tag"):
        _validate_wheel(wheel, "fixture-dependency", "1.2.3", "win_amd64")


def test_authoritative_acquisition_uses_exact_binary_only_pypi_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[str] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        recorded.extend(command)
        destination = Path(command[command.index("--dest") + 1])
        _wheel(
            destination / "fixture_dependency-1.2.3-cp312-cp312-win_amd64.whl",
            "fixture-dependency",
            "1.2.3",
            tags=("cp312-cp312-win_amd64",),
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _download(
        tmp_path,
        "fixture-dependency==1.2.3",
        "win_amd64",
        {"implementation": "cp", "abi": "cp312"},
    )

    assert result.name == "fixture_dependency-1.2.3-cp312-cp312-win_amd64.whl"
    assert recorded[0:4] == [sys._base_executable, "-m", "pip", "download"]
    assert "fixture-dependency==1.2.3" in recorded
    for option in (
        "--only-binary=:all:",
        "--no-deps",
        "--python-version",
        "--implementation",
        "--abi",
        "--platform",
        "--index-url",
        "https://pypi.org/simple",
    ):
        assert option in recorded


def test_build_rejects_symlinked_skill_input(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wheel, cache = _write_fixture_repo(repo)
    linked = repo / "plugins/csaf/skills/csaf/linked.md"
    try:
        linked.symlink_to(repo / "plugins/csaf/skills/csaf/SKILL.md")
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ReleaseBuildError, match="symlink"):
        build_release(
            repo_root=repo,
            output_root=tmp_path / "out",
            wheel=wheel,
            wheelhouse_root=cache,
            requested_version=VERSION,
            require_clean=False,
        )


def test_native_install_verifier_fails_closed_on_asset_mismatch(tmp_path: Path) -> None:
    asset = tmp_path / "asset.bin"
    asset.write_bytes(b"verified bytes")

    with pytest.raises(NativeVerificationError, match="verification"):
        _verified_asset(asset, sha256="0" * 64, size=asset.stat().st_size)
    with pytest.raises(NativeVerificationError, match="verification"):
        _verified_asset(asset, sha256=hashlib.sha256(asset.read_bytes()).hexdigest(), size=1)


def test_verifier_cli_sanitizes_unexpected_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**_: object) -> dict[str, object]:
        raise RuntimeError("credential-looking detail must-not-leak")

    monkeypatch.setattr(native_verifier, "verify_native_install", fail)
    code = native_verifier.main(
        [
            "--release-dir",
            str(tmp_path),
            "--dependencies",
            str(tmp_path / "dependencies.json"),
            "--platform",
            "windows-x64",
            "--uv",
            str(tmp_path / "uv.zip"),
            "--officecli",
            str(tmp_path / "officecli.exe"),
            "--csaf",
            str(tmp_path / "csaf.exe"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == "native install verification failed\n"
    assert "must-not-leak" not in captured.err


def test_verifier_cli_reports_only_allowlisted_failure_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**_: object) -> dict[str, object]:
        raise NativeVerificationError(
            "credential-looking detail must-not-leak", diagnostic="setup-install"
        )

    monkeypatch.setattr(native_verifier, "verify_native_install", fail)
    code = native_verifier.main(
        [
            "--release-dir",
            str(tmp_path),
            "--dependencies",
            str(tmp_path / "dependencies.json"),
            "--platform",
            "linux-x64",
            "--uv",
            str(tmp_path / "uv.tar.gz"),
            "--officecli",
            str(tmp_path / "officecli"),
            "--csaf",
            str(tmp_path / "csaf"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == "native install verification failed [setup-install]\n"
    assert "must-not-leak" not in captured.err


def test_native_verifier_prepares_private_posix_data_root(tmp_path: Path) -> None:
    data = tmp_path / "data"

    bin_directory = native_verifier._prepare_data_root(data)

    assert bin_directory == data / "bin"
    assert bin_directory.is_dir()
    if os.name == "posix":
        assert stat.S_IMODE(data.stat().st_mode) == 0o700
        assert stat.S_IMODE(bin_directory.stat().st_mode) == 0o700


def test_egress_sitecustomize_refuses_direct_external_socket(tmp_path: Path) -> None:
    guard = native_verifier._write_egress_guard(tmp_path / "guard")
    if os.name == "posix":
        assert stat.S_IMODE(guard.parent.stat().st_mode) == 0o700
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(guard.parent)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,socket; assert os.environ['CSAF_EGRESS_GUARD_ACTIVE']=='1'; "
            "s=socket.socket(); "
            "\ntry: s.connect(('203.0.113.1', 443))\n"
            "except PermissionError: print('CSAF_EGRESS_BLOCKED')\n"
            "else: raise SystemExit('external socket unexpectedly allowed')",
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "CSAF_EGRESS_BLOCKED"


def test_guard_probe_leaves_policy_directory_exact_for_activated_environment(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "data/versions/0.1.0"
    package = runtime / "site-packages/csaf"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("RUNTIME_COPY = True\n", encoding="utf-8")
    launcher = runtime / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"launcher")
    if os.name != "nt":
        launcher.chmod(0o700)
    guard = native_verifier._write_egress_guard(tmp_path / "guard")
    environment = native_verifier._controlled_environment(
        runtime / "site-packages", guard.parent, {"PYTHONPATH": str(tmp_path / "checkout")}
    )

    result = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ['CSAF_EGRESS_GUARD_ACTIVE'])"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert {path.name for path in guard.parent.iterdir()} == {"sitecustomize.py"}
    activated = native_verifier._activated_environment(launcher, guard.parent, environment)
    assert activated["PYTHONDONTWRITEBYTECODE"] == "1"


def test_console_environment_imports_only_from_smoke_site_packages(tmp_path: Path) -> None:
    site_packages = tmp_path / "smoke/site-packages"
    package = site_packages / "csaf"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("SMOKE_COPY = True\n", encoding="utf-8")
    guard = native_verifier._write_egress_guard(tmp_path / "guard")
    shadow = tmp_path / "checkout"
    (shadow / "csaf").mkdir(parents=True)
    (shadow / "csaf/__init__.py").write_text("SMOKE_COPY = False\n", encoding="utf-8")

    environment = native_verifier._console_environment(
        site_packages, guard.parent, {"PYTHONPATH": str(shadow)}
    )
    result = subprocess.run(
        [sys.executable, "-c", "import csaf; print(csaf.__file__)"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    Path(result.stdout.strip()).resolve(strict=True).relative_to(site_packages.resolve(strict=True))
    assert environment["PYTHONPATH"] == str(site_packages) + os.pathsep + str(guard.parent)
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert str(shadow) not in environment["PYTHONPATH"]


def test_console_origin_is_proved_before_installed_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_packages = tmp_path / "smoke/site-packages"
    (site_packages / "csaf").mkdir(parents=True)
    (site_packages / "csaf/__init__.py").write_text("", encoding="utf-8")
    console = tmp_path / ("csaf.exe" if os.name == "nt" else "csaf")
    console.write_bytes(b"console")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    proof = tmp_path / "proof"
    bootstrap_python = tmp_path / "bootstrap/bin/python"
    bootstrap_console = tmp_path / "bootstrap/bin/csaf"
    calls: list[str] = []

    def fake_bootstrap(
        root: Path, source_console: Path, python_executable: Path
    ) -> tuple[Path, Path]:
        assert root == tmp_path / "bootstrap"
        assert source_console == console
        assert python_executable == Path(sys.executable)
        bootstrap_python.parent.mkdir(parents=True)
        bootstrap_python.write_bytes(b"python")
        bootstrap_console.write_bytes(b"console")
        return bootstrap_python, bootstrap_console

    def fake_run(
        command: list[str], *, env: dict[str, str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        del env, timeout
        if command[0] == sys.executable:
            calls.append("origin")
            proof.write_text("CSAF_EGRESS_GUARD_ACTIVE\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "CSAF_RUNTIME_IMPORT_OK", "")
        calls.append("console")
        assert command[:2] == [str(bootstrap_python), str(bootstrap_console)]
        assert not proof.exists()
        proof.write_text("CSAF_EGRESS_GUARD_ACTIVE\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "ready", "")

    monkeypatch.setattr(native_verifier, "_run", fake_run)
    monkeypatch.setattr(native_verifier, "_prepare_console_bootstrap", fake_bootstrap)

    result = native_verifier._run_installed_console(
        console=console,
        manifest=manifest,
        site_packages=site_packages,
        env={},
        proof=proof,
        bootstrap_root=tmp_path / "bootstrap",
        platform="linux-x64",
    )

    assert result.returncode == 0
    assert calls == ["origin", "console"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv interpreters use symbolic links")
def test_console_bootstrap_replaces_posix_venv_symlinks_with_regular_files(
    tmp_path: Path,
) -> None:
    smoke = tmp_path / "smoke"
    scripts = smoke / "bin"
    scripts.mkdir(parents=True)
    source_python = scripts / "python"
    source_python.symlink_to(Path(sys.executable).resolve(strict=True))
    source_console = scripts / "csaf"
    source_console.write_bytes(b"console")
    source_console.chmod(0o700)
    (smoke / "pyvenv.cfg").write_text(
        f"home = {Path(sys._base_executable).resolve(strict=True).parent}\n",
        encoding="utf-8",
    )

    bootstrap_python, bootstrap_console = native_verifier._prepare_console_bootstrap(
        tmp_path / "bootstrap", source_console, source_python
    )

    assert bootstrap_python == tmp_path / "bootstrap/bin/python"
    assert bootstrap_console == tmp_path / "bootstrap/bin/csaf"
    assert native_verifier._regular(bootstrap_python)
    assert native_verifier._regular(bootstrap_console)
    assert native_verifier._regular(tmp_path / "bootstrap/pyvenv.cfg")
    assert stat.S_IMODE((tmp_path / "bootstrap").stat().st_mode) == 0o700
    assert stat.S_IMODE(bootstrap_python.stat().st_mode) == 0o700
    assert stat.S_IMODE(bootstrap_console.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "bootstrap/pyvenv.cfg").stat().st_mode) == 0o600
    assert bootstrap_python.read_bytes() == Path(sys.executable).resolve(strict=True).read_bytes()
    assert bootstrap_console.read_bytes() == b"console"
    assert (tmp_path / "bootstrap/pyvenv.cfg").read_bytes() == (
        f"home = {Path(sys._base_executable).resolve(strict=True).parent}\n".encode()
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX O_NOFOLLOW protects bootstrap sources")
def test_console_bootstrap_refuses_source_replaced_by_symlink_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = tmp_path / "smoke"
    scripts = smoke / "bin"
    scripts.mkdir(parents=True)
    source_python = scripts / "python"
    source_python.symlink_to(Path(sys.executable).resolve(strict=True))
    source_console = scripts / "csaf"
    source_console.write_bytes(b"trusted-console")
    source_console.chmod(0o700)
    (smoke / "pyvenv.cfg").write_text(
        f"home = {Path(sys._base_executable).resolve(strict=True).parent}\n",
        encoding="utf-8",
    )
    attacker = tmp_path / "attacker"
    attacker.write_bytes(b"attacker-controlled")
    original_open = os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and Path(path) == source_console:
            source_console.rename(source_console.with_suffix(".trusted"))
            source_console.symlink_to(attacker)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_open)

    with pytest.raises(
        NativeVerificationError,
        match="^installed console bootstrap verification failed$",
    ):
        native_verifier._prepare_console_bootstrap(
            tmp_path / "bootstrap", source_console, source_python
        )

    assert swapped
    assert not (tmp_path / "bootstrap/bin/csaf").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptors identify bootstrap sources")
def test_console_bootstrap_refuses_source_replaced_by_regular_file_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = tmp_path / "smoke"
    scripts = smoke / "bin"
    scripts.mkdir(parents=True)
    source_python = scripts / "python"
    source_python.symlink_to(Path(sys.executable).resolve(strict=True))
    source_console = scripts / "csaf"
    source_console.write_bytes(b"trusted-console")
    (smoke / "pyvenv.cfg").write_text("home = /trusted\n", encoding="utf-8")
    original_open = os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and Path(path) == source_console:
            source_console.rename(source_console.with_suffix(".trusted"))
            source_console.write_bytes(b"attacker-controlled")
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_open)

    with pytest.raises(
        NativeVerificationError,
        match="^installed console bootstrap verification failed$",
    ):
        native_verifier._prepare_console_bootstrap(
            tmp_path / "bootstrap", source_console, source_python
        )

    assert swapped
    assert not (tmp_path / "bootstrap/bin/csaf").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptors identify bootstrap sources")
def test_console_bootstrap_refuses_same_inode_mutation_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = tmp_path / "smoke"
    scripts = smoke / "bin"
    scripts.mkdir(parents=True)
    source_python = scripts / "python"
    source_python.symlink_to(Path(sys.executable).resolve(strict=True))
    source_console = scripts / "csaf"
    source_console.write_bytes(b"trusted-console")
    (smoke / "pyvenv.cfg").write_text("home = /trusted\n", encoding="utf-8")
    original_identity = source_console.stat().st_ino
    original_open = os.open
    mutated = False

    def mutate_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal mutated
        if not mutated and Path(path) == source_console:
            source_console.write_bytes(b"attacker-controlled-content")
            assert source_console.stat().st_ino == original_identity
            mutated = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", mutate_before_open)

    with pytest.raises(
        NativeVerificationError,
        match="^installed console bootstrap verification failed$",
    ):
        native_verifier._prepare_console_bootstrap(
            tmp_path / "bootstrap", source_console, source_python
        )

    assert mutated
    assert not (tmp_path / "bootstrap/bin/csaf").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptors identify bootstrap sources")
def test_console_bootstrap_refuses_ancestor_replaced_before_source_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = tmp_path / "smoke"
    scripts = smoke / "bin"
    scripts.mkdir(parents=True)
    source_python = scripts / "python"
    source_python.symlink_to(Path(sys.executable).resolve(strict=True))
    source_console = scripts / "csaf"
    source_console.write_bytes(b"trusted-console")
    (smoke / "pyvenv.cfg").write_text("home = /trusted\n", encoding="utf-8")
    original_open = os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and Path(path) == source_console:
            smoke.rename(tmp_path / "smoke.trusted")
            scripts.mkdir(parents=True)
            source_console.write_bytes(b"attacker-controlled")
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_open)

    with pytest.raises(
        NativeVerificationError,
        match="^installed console bootstrap verification failed$",
    ):
        native_verifier._prepare_console_bootstrap(
            tmp_path / "bootstrap", source_console, source_python
        )

    assert swapped
    assert not (tmp_path / "bootstrap/bin/csaf").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX bootstrap uses raw descriptors")
def test_console_bootstrap_sanitizes_descriptor_close_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = tmp_path / "smoke"
    scripts = smoke / "bin"
    scripts.mkdir(parents=True)
    source_python = scripts / "python"
    source_python.symlink_to(Path(sys.executable).resolve(strict=True))
    source_console = scripts / "csaf"
    source_console.write_bytes(b"console")
    (smoke / "pyvenv.cfg").write_text("home = /trusted\n", encoding="utf-8")
    original_close = os.close
    failed = False

    def fail_once_after_close(file_descriptor: int) -> None:
        nonlocal failed
        original_close(file_descriptor)
        if not failed:
            failed = True
            raise OSError("sensitive descriptor detail")

    monkeypatch.setattr(os, "close", fail_once_after_close)

    with pytest.raises(
        NativeVerificationError,
        match="^installed console bootstrap verification failed$",
    ):
        native_verifier._prepare_console_bootstrap(
            tmp_path / "bootstrap", source_console, source_python
        )

    assert failed


def test_verifier_workspace_is_created_beside_validated_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release/0.1.0"
    release.mkdir(parents=True)
    sentinel = object()
    captured: dict[str, object] = {}

    def fake_temporary_directory(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(tempfile, "TemporaryDirectory", fake_temporary_directory)

    result = native_verifier._verification_workspace(release)

    assert result is sentinel
    assert captured == {"prefix": "csaf-native-ready-", "dir": release.parent}


def _activated_state(data: Path, runtime: Path, version: str) -> None:
    (data / "current.json").write_text(
        json.dumps({"schema_version": 1, "active_version": version, "runtime_path": str(runtime)}),
        encoding="utf-8",
    )
    (data / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_version": version,
                "installed_versions": [version],
                "runtime_paths": {version: str(runtime)},
                "verified_checksums": {},
                "adapter_targets": {},
                "officecli_installed_by_csaf": False,
            }
        ),
        encoding="utf-8",
    )


def test_activated_runtime_rejects_broken_current_path(tmp_path: Path) -> None:
    data = tmp_path / "data"
    expected = data / "versions/0.1.0"
    expected.mkdir(parents=True)
    launcher = expected / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"launcher")
    if os.name != "nt":
        launcher.chmod(0o700)
    _activated_state(data, tmp_path / "outside", "0.1.0")
    with pytest.raises(NativeVerificationError, match="activated runtime"):
        native_verifier._activated_launcher(
            data, "0.1.0", "windows-x64" if os.name == "nt" else "linux-x64"
        )


def test_activated_doctor_runs_through_runtime_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    runtime = data / "versions/0.1.0"
    runtime.mkdir(parents=True)
    site_packages = runtime / "site-packages"
    site_packages.mkdir()
    (site_packages / "csaf").mkdir()
    (site_packages / "csaf/__init__.py").write_text("", encoding="utf-8")
    launcher = runtime / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"launcher")
    if os.name != "nt":
        launcher.chmod(0o700)
    _activated_state(data, runtime, "0.1.0")
    proof = tmp_path / "proof"
    proof.write_text("CSAF_EGRESS_GUARD_ACTIVE\n", encoding="utf-8")
    guard = native_verifier._write_egress_guard(tmp_path / "guard")
    inherited = tmp_path / "mutable-checkout"
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []

    def fake_run(
        command: list[str], *, env: dict[str, str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        commands.append(command)
        environments.append(env)
        if command[0] == sys.executable:
            proof.write_text("CSAF_EGRESS_GUARD_ACTIVE\n", encoding="utf-8")
            output = "CSAF_RUNTIME_IMPORT_OK"
        else:
            if "--help" in command:
                assert not proof.exists()
                proof.write_text("CSAF_EGRESS_GUARD_ACTIVE\n", encoding="utf-8")
            output = '{"status":"ready"}' if "doctor" in command else "0.1.0"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(native_verifier, "_run", fake_run)
    report = native_verifier._verify_activated_runtime(
        data=data,
        version="0.1.0",
        platform="windows-x64" if os.name == "nt" else "linux-x64",
        env={
            "CSAF_EGRESS_GUARD_PROOF": str(proof),
            "PYTHONPATH": str(guard.parent) + os.pathsep + str(inherited),
        },
        guard=guard.parent,
        proof=proof,
    )
    assert report == {"status": "ready"}
    assert commands[0][0] == sys.executable
    assert commands[-1] == [str(launcher), "--database", ":memory:", "setup", "doctor", "--json"]
    assert all(command[0] == str(launcher) for command in commands[1:])
    expected_pythonpath = str(site_packages) + os.pathsep + str(guard.parent)
    assert all(environment["PYTHONPATH"] == expected_pythonpath for environment in environments)
    assert all(environment["PYTHONNOUSERSITE"] == "1" for environment in environments)
    assert all(str(inherited) not in environment["PYTHONPATH"] for environment in environments)


def test_activated_environment_imports_csaf_only_from_versioned_site_packages(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "data/versions/0.1.0"
    site_packages = runtime / "site-packages"
    package = site_packages / "csaf"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("RUNTIME_COPY = True\n", encoding="utf-8")
    launcher = runtime / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"launcher")
    if os.name != "nt":
        launcher.chmod(0o700)
    guard = native_verifier._write_egress_guard(tmp_path / "guard")
    shadow = tmp_path / "shadow"
    (shadow / "csaf").mkdir(parents=True)
    (shadow / "csaf/__init__.py").write_text("RUNTIME_COPY = False\n", encoding="utf-8")

    environment = native_verifier._activated_environment(
        launcher,
        guard.parent,
        {
            "PYTHONPATH": str(guard.parent) + os.pathsep + str(shadow),
            "CSAF_EGRESS_GUARD_PROOF": str(tmp_path / "proof"),
        },
    )
    result = subprocess.run(
        [sys.executable, "-c", "import csaf; print(csaf.__file__)"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    origin = Path(result.stdout.strip()).resolve(strict=True)
    origin.relative_to(site_packages.resolve(strict=True))
    assert str(shadow) not in environment["PYTHONPATH"]
    assert (tmp_path / "proof").read_text(encoding="utf-8") == "CSAF_EGRESS_GUARD_ACTIVE\n"


def test_uv_extraction_accepts_only_the_pinned_archive_layout(tmp_path: Path) -> None:
    archive = tmp_path / "uv.zip"
    with zipfile.ZipFile(archive, "w") as opened:
        for name in ("uv.exe", "uvw.exe", "uvx.exe"):
            opened.writestr(name, name.encode())

    executable = _extract_uv(
        archive,
        tmp_path / "valid",
        "windows-x64",
        "https://example.test/uv-x86_64-pc-windows-msvc.zip",
    )

    assert executable == tmp_path / "valid/uv.exe"
    assert executable.read_bytes() == b"uv.exe"

    with zipfile.ZipFile(tmp_path / "extra.zip", "w") as opened:
        for name in ("uv.exe", "uvw.exe", "uvx.exe", "unexpected.exe"):
            opened.writestr(name, name.encode())
    with pytest.raises(NativeVerificationError, match="archive verification"):
        _extract_uv(
            tmp_path / "extra.zip",
            tmp_path / "extra",
            "windows-x64",
            "https://example.test/uv-x86_64-pc-windows-msvc.zip",
        )


def test_uv_extraction_rejects_archive_links(tmp_path: Path) -> None:
    archive = tmp_path / "linked.zip"
    with zipfile.ZipFile(archive, "w") as opened:
        opened.writestr("uv.exe", b"uv")
        opened.writestr("uvw.exe", b"uvw")
        linked = zipfile.ZipInfo("uvx.exe")
        linked.create_system = 3
        linked.external_attr = 0o120777 << 16
        opened.writestr(linked, "uv.exe")

    with pytest.raises(NativeVerificationError, match="archive verification"):
        _extract_uv(
            archive,
            tmp_path / "linked",
            "windows-x64",
            "https://example.test/uv-x86_64-pc-windows-msvc.zip",
        )


def test_ci_runs_full_trusted_https_ready_verifier() -> None:
    root = Path(__file__).parents[2]
    source = (root / ".github/workflows/ci.yml").read_text("utf-8")
    verifier = (root / "scripts/verify_native_install.py").read_text("utf-8")

    assert "scripts/verify_native_install.py" in source
    assert (
        "${{ matrix.verifier-python }} -m pip install --require-hashes "
        "-r requirements/release-tools.txt"
    ) in source
    assert "uv-archive" in source
    assert "SSL_CERT_FILE" in verifier
    assert "HTTPS_PROXY" in verifier
    assert "NO_PROXY" in verifier
    assert '"--codex-only"' in verifier
    assert 'report.get("status") != "ready"' in verifier
