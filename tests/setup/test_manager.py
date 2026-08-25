from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import time
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from csaf.setup import (
    AdapterInstallResult,
    AssistantKind,
    ReleaseManifest,
    SetupError,
    SupportedPlatform,
    Version,
)
from csaf.setup.manager import (
    ClaudeManagedAdapter,
    CodexManagedAdapter,
    GeminiManagedAdapter,
    SetupManager,
    SetupStatus,
)

PLATFORM = SupportedPlatform.WINDOWS_X64 if os.name == "nt" else SupportedPlatform.LINUX_X64


def _secure_posix_fixture(path: Path, *, executable: bool = False) -> None:
    if os.name != "nt":
        os.chmod(path, 0o700 if path.is_dir() or executable else 0o600)


def _asset(payload: bytes, name: str) -> dict[str, Any]:
    return {
        "url": f"https://example.test/{name}",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def _manifest(version: str = "0.1.0") -> ReleaseManifest:
    runtime = b"runtime-wheel"
    officecli = b"officecli-binary"
    runtime_assets = {
        platform.value: _asset(runtime, f"runtime-{platform.value}")
        for platform in SupportedPlatform
    }
    office_assets = {
        platform.value: _asset(officecli, f"officecli-{platform.value}")
        for platform in SupportedPlatform
    }
    return ReleaseManifest.model_validate(
        {
            "schema_version": 1,
            "version": version,
            "runtime": runtime_assets,
            "codex_skill": _asset(b"codex", "codex.zip"),
            "claude_plugin": _asset(b"claude", "claude.zip"),
            "officecli": {
                "version": "1.0.143",
                "minimum_version": "1.0.137",
                "assets": office_assets,
            },
        }
    )


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.effects: list[tuple[str, object]] = []
        self.fail: str | None = None
        self.doctor_ready = True
        self.writer_calls = 0
        self.runtime_versions: list[Version] = []

    def download(self, asset: object, destination: Path) -> Path:
        self.effects.append(("download", asset))
        if self.fail == "download":
            raise SetupError("asset download failed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = str(getattr(asset, "url", ""))
        payload = (
            b"officecli-binary"
            if "officecli" in url
            else b"codex"
            if "codex" in url
            else b"claude"
            if "claude" in url
            else b"runtime-wheel"
        )
        destination.write_bytes(payload)
        return destination

    def install_runtime(self, wheel: Path, destination: Path, expected_version: Version) -> Path:
        self.effects.append(("runtime", wheel))
        self.runtime_versions.append(expected_version)
        if self.fail == "runtime":
            raise SetupError("runtime staging failed")
        destination.mkdir(parents=True)
        launcher = destination / ("csaf.exe" if os.name == "nt" else "csaf")
        launcher.write_bytes(b"launcher")
        _secure_posix_fixture(destination)
        _secure_posix_fixture(launcher, executable=True)
        return launcher

    def runtime_probe(self, runtime: Path, _version: object) -> bool:
        launcher = runtime / ("csaf.exe" if os.name == "nt" else "csaf")
        return launcher.is_file() and launcher.read_bytes() == b"launcher"

    def officecli_probe(self, officecli: Path, version: object, minimum: object) -> bool:
        return (
            str(version) == "1.0.143"
            and str(minimum) >= "1.0.137"
            and officecli.is_file()
            and officecli.read_bytes() == b"officecli-binary"
        )

    def doctor(self, runtime: Path, officecli: Path, environment: dict[str, str]) -> bool:
        self.effects.append(("doctor", (runtime, officecli, dict(environment))))
        if self.fail == "doctor":
            raise SetupError("setup diagnostics failed")
        return self.doctor_ready

    def write_json(self, path: Path, value: object) -> None:
        from csaf.setup.assets import write_json_atomic

        self.writer_calls += 1
        self.effects.append(("write", path.name))
        if self.fail == "state" and path.name == "state.json":
            raise SetupError("could not persist installation state")
        write_json_atomic(path, value)


class Adapter:
    def __init__(
        self,
        kind: AssistantKind,
        effects: list[tuple[str, object]],
        destination: Path,
    ) -> None:
        self.kind = kind
        self.effects = effects
        self.destination = destination
        self.fail = False
        self.activated_failure = False
        self.health_ready = True
        self.uninstall_failure = False
        self.calls = 0

    def install(self, asset: Path, version: object) -> AdapterInstallResult:
        self.calls += 1
        self.effects.append(("adapter", (self.kind, asset.read_bytes(), str(version))))
        if self.fail:
            raise SetupError(
                "token=secret-value C:\\Users\\Alice\\adapter.log",
                activated=self.activated_failure,
            )
        self.destination.mkdir(parents=True, exist_ok=True)
        marker = self.destination / "asset.bin"
        version_marker = self.destination / "version.txt"
        marker.write_bytes(asset.read_bytes())
        version_marker.write_text(str(version), encoding="ascii")
        _secure_posix_fixture(self.destination)
        _secure_posix_fixture(marker)
        _secure_posix_fixture(version_marker)
        return AdapterInstallResult(self.kind, self.destination)

    def health(self, target: Path, version: object, sha256: str) -> bool:
        marker = target / "asset.bin"
        return (
            self.health_ready
            and target == self.destination
            and (target / "version.txt").read_text(encoding="ascii") == str(version)
            and marker.is_file()
            and hashlib.sha256(marker.read_bytes()).hexdigest() == sha256
        )

    def uninstall(self, target: Path) -> None:
        self.effects.append(("adapter-uninstall", self.kind))
        if self.uninstall_failure:
            raise SetupError("token=secret-value C:\\Users\\Alice\\uninstall.log")
        if target.is_dir():
            import shutil

            shutil.rmtree(target)


def _manager(
    tmp_path: Path,
    harness: Harness,
    *,
    detected: tuple[AssistantKind, ...] = (AssistantKind.CODEX, AssistantKind.CLAUDE),
) -> tuple[SetupManager, dict[AssistantKind, Adapter]]:
    adapters = {
        kind: Adapter(
            kind,
            harness.effects,
            tmp_path / "data" / "adapters" / kind.value,
        )
        for kind in detected
    }
    manager = SetupManager(
        data_root=tmp_path / "data",
        platform=PLATFORM,
        detected_assistants=detected,
        downloader=harness.download,
        runtime_installer=harness.install_runtime,
        adapter_installers=adapters,
        doctor_runner=harness.doctor,
        runtime_probe=harness.runtime_probe,
        officecli_probe=harness.officecli_probe,
        json_writer=harness.write_json,
        clock=lambda: datetime(2026, 8, 11, tzinfo=UTC),
    )
    return manager, adapters


def _active(root: Path, version: str = "0.0.9") -> None:
    from csaf.setup.assets import write_json_atomic

    runtime = root / "versions" / version
    runtime.mkdir(parents=True)
    launcher = runtime / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"old")
    for directory in (root, runtime.parent, runtime):
        _secure_posix_fixture(directory)
    _secure_posix_fixture(launcher, executable=True)
    write_json_atomic(
        root / "current.json",
        {"schema_version": 1, "active_version": version, "runtime_path": str(runtime)},
    )


def test_planning_is_read_only_and_selects_every_detected_assistant(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    detected = (AssistantKind.CODEX, AssistantKind.CLAUDE, AssistantKind.GEMINI)
    manager, _ = _manager(tmp_path, harness, detected=detected)

    plan = manager.plan_install(_manifest(), requested_targets=None)

    assert plan.targets == detected
    assert plan.adapter_assets[AssistantKind.GEMINI] == plan.manifest.codex_skill
    assert plan.officecli_asset == plan.manifest.officecli.assets[PLATFORM]
    assert harness.effects == []
    assert not (tmp_path / "data").exists()


def test_requested_target_must_have_been_detected(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path, Harness(tmp_path), detected=(AssistantKind.CODEX,))

    with pytest.raises(SetupError, match="requested assistant was not detected"):
        manager.plan_install(_manifest(), requested_targets=(AssistantKind.CLAUDE,))


def test_declined_install_performs_no_material_write(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness)
    plan = manager.plan_install(_manifest(), requested_targets=None)

    result = manager.install(plan, consent=lambda _: False)

    assert result.status is SetupStatus.CANCELLED
    assert result.activated is False
    assert harness.effects == []
    assert not (tmp_path / "data").exists()


def test_assume_yes_bypasses_only_prompt_and_installs_exact_manifest_assets(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    prompted = False

    def consent(_plan: object) -> bool:
        nonlocal prompted
        prompted = True
        return False

    plan = manager.plan_install(_manifest(), requested_targets=None)
    result = manager.install(plan, consent=consent, assume_yes=True)

    assert result.status is SetupStatus.READY
    assert prompted is False
    assert harness.runtime_versions == [plan.manifest.version]
    assert [effect[1] for effect in harness.effects if effect[0] == "download"] == [
        plan.runtime_asset,
        plan.officecli_asset,
        plan.adapter_assets[AssistantKind.CODEX],
        plan.adapter_assets[AssistantKind.CLAUDE],
    ]
    assert all(adapter.calls == 1 for adapter in adapters.values())
    doctor_environment = next(effect[1][2] for effect in harness.effects if effect[0] == "doctor")
    assert doctor_environment["CSAF_OFFICECLI"].endswith(
        "officecli.exe" if os.name == "nt" else "officecli"
    )
    assert doctor_environment["OFFICECLI_SKIP_UPDATE"] == "1"
    assert doctor_environment["OFFICECLI_RESIDENT_FLUSH"] == "each"
    assert (tmp_path / "data" / "current.json").is_file()


def test_diagnostics_run_before_state_and_activation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness)

    result = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )

    names = [effect[0] for effect in harness.effects]
    assert result.status is SetupStatus.READY
    assert names.index("doctor") < names.index("write")
    assert names[-1] == "write"
    assert harness.effects[-1][1] == "current.json"
    assert ("write", "current.json") in harness.effects


def test_production_doctor_forces_versioned_site_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from csaf.setup.manager import _doctor

    runtime = tmp_path / "runtime"
    site_packages = runtime / "site-packages"
    site_packages.mkdir(parents=True)
    launcher = runtime / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"launcher")
    officecli = tmp_path / ("officecli.exe" if os.name == "nt" else "officecli")
    officecli.write_bytes(b"office")
    captured: dict[str, str] = {}

    def run(_arguments: object, **options: object) -> object:
        captured.update(options["env"])  # type: ignore[arg-type]
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("csaf.setup.manager.subprocess.run", run)

    assert _doctor(runtime, officecli, {"PYTHONPATH": str(tmp_path / "mutable-bootstrap")})
    assert captured["PYTHONPATH"] == str(site_packages)
    assert captured["PYTHONNOUSERSITE"] == "1"


def test_production_runtime_probe_requires_complete_versioned_closure(tmp_path: Path) -> None:
    from csaf.setup.manager import _runtime_probe

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ("csaf.exe" if os.name == "nt" else "csaf")).write_bytes(b"launcher")

    assert _runtime_probe(runtime, Version("0.1.0")) is False

    site_packages = runtime / "site-packages"
    (site_packages / "csaf").mkdir(parents=True)
    (site_packages / "csaf" / "__init__.py").write_text("", encoding="utf-8")
    metadata = site_packages / "csaf-0.1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: csaf\nVersion: 0.1.0\n", encoding="utf-8")

    assert _runtime_probe(runtime, Version("0.1.0")) is True


@pytest.mark.parametrize("failure", ["download", "runtime", "doctor", "state"])
def test_failure_before_activation_keeps_existing_version_and_cleans_staging(
    tmp_path: Path,
    failure: str,
) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    _active(tmp_path / "data")
    harness.fail = failure

    result = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status is SetupStatus.FAILED
    assert result.activated is False
    assert '"active_version":"0.0.9"' in (tmp_path / "data" / "current.json").read_text()
    staging = tmp_path / "data" / ".staging"
    assert not staging.exists() or list(staging.iterdir()) == []


def test_activation_failure_after_backup_restores_previous_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    _active(tmp_path / "data", version="0.1.0")
    plan = manager.plan_install(_manifest(), requested_targets=None)
    old_launcher = plan.runtime_path / ("csaf.exe" if os.name == "nt" else "csaf")
    original_replace = os.replace

    def fail_new_runtime(source: object, target: object) -> None:
        if Path(source).name == "runtime" and Path(target) == plan.runtime_path:
            raise OSError("activation interrupted")
        original_replace(source, target)

    monkeypatch.setattr("csaf.setup.manager.os.replace", fail_new_runtime)

    result = manager.install(plan, consent=lambda _: True)

    assert result.status is SetupStatus.FAILED
    assert old_launcher.read_bytes() == b"old"
    assert '"active_version":"0.1.0"' in (tmp_path / "data" / "current.json").read_text()


def test_forged_plan_cannot_redirect_activation_outside_data_root(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    forged = replace(
        manager.plan_install(_manifest(), requested_targets=None),
        runtime_path=tmp_path / "outside",
    )

    with pytest.raises(SetupError, match="installation plan destinations are invalid"):
        manager.install(forged, consent=lambda _: True)

    assert harness.effects == []
    assert not (tmp_path / "outside").exists()


def test_adapter_failure_rolls_back_and_keeps_existing_version_selected(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    _active(tmp_path / "data")
    adapters[AssistantKind.CLAUDE].fail = True
    result = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )
    assert result.status is SetupStatus.FAILED
    assert result.activated is False
    assert '"active_version":"0.0.9"' in (tmp_path / "data" / "current.json").read_text()


def test_adapter_activated_error_is_cleaned_when_uninstall_verifies_removal(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness, detected=(AssistantKind.CODEX,))
    adapters[AssistantKind.CODEX].fail = True
    adapters[AssistantKind.CODEX].activated_failure = True
    result = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )
    assert result.status is SetupStatus.FAILED
    assert result.activated is False
    assert result.error == "assistant adapter installation failed"


def test_repeat_install_of_healthy_active_version_is_idempotent(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    harness.effects.clear()

    second_plan = manager.plan_install(_manifest(), requested_targets=None)
    result = manager.install(second_plan, consent=lambda _: pytest.fail("prompted"))

    assert result.status is SetupStatus.READY
    assert result.activated is True
    assert harness.effects == []
    assert all(adapter.calls == 1 for adapter in adapters.values())


def test_health_check_rejects_same_size_officecli_corruption(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    plan.officecli_path.write_bytes(b"x" * plan.officecli_asset.size)

    assert manager.plan_install(_manifest(), requested_targets=None).already_healthy is False


def test_health_check_requires_verified_runtime_marker(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    marker = plan.runtime_path / ".csaf-runtime.sha256"
    marker.write_text("0" * 64, encoding="ascii")

    assert manager.plan_install(_manifest(), requested_targets=None).already_healthy is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_install_secures_runtime_launcher_and_marker(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    plan = manager.plan_install(_manifest(), requested_targets=None)

    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY

    launcher = plan.runtime_path / "csaf"
    marker = plan.runtime_path / ".csaf-runtime.sha256"
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o700
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_doctor_failure_is_sanitized(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())

    def unsafe_doctor(_runtime: Path, _officecli: Path, _environment: dict[str, str]) -> bool:
        raise OSError("token=secret-value C:\\Users\\Alice\\private.log")

    manager = SetupManager(
        data_root=tmp_path / "data",
        platform=PLATFORM,
        detected_assistants=(),
        downloader=harness.download,
        runtime_installer=harness.install_runtime,
        adapter_installers={},
        doctor_runner=unsafe_doctor,
    )

    result = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status is SetupStatus.FAILED
    assert result.error == "setup diagnostics failed"
    assert "secret-value" not in result.error
    assert "Alice" not in result.error


def test_repair_reinstalls_a_damaged_matching_version(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    runtime = tmp_path / "data" / "versions" / "0.1.0"
    (runtime / ("csaf.exe" if os.name == "nt" else "csaf")).unlink()
    harness.effects.clear()

    result = manager.repair(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status is SetupStatus.READY
    assert any(effect[0] == "download" for effect in harness.effects)


def test_plan_binds_adapter_assets_and_discloses_exact_destinations(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)

    plan = manager.plan_install(_manifest(), requested_targets=None)

    assert plan.adapter_assets == {
        AssistantKind.CODEX: plan.manifest.codex_skill,
        AssistantKind.CLAUDE: plan.manifest.claude_plugin,
    }
    assert plan.adapter_destinations == {
        kind: adapter.destination for kind, adapter in adapters.items()
    }


def test_verified_adapter_assets_are_passed_with_consented_version(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness)
    plan = manager.plan_install(_manifest(), requested_targets=None)

    result = manager.install(plan, consent=lambda _: True)

    assert result.status is SetupStatus.READY
    adapter_effects = [value for name, value in harness.effects if name == "adapter"]
    assert adapter_effects == [
        (AssistantKind.CODEX, b"codex", "0.1.0"),
        (AssistantKind.CLAUDE, b"claude", "0.1.0"),
    ]


@pytest.mark.parametrize(
    ("version", "minimum"),
    [("1.0.142", "1.0.137"), ("1.0.144", "1.0.137"), ("1.0.143", "1.0.136")],
)
def test_hard_officecli_policy_rejects_manifest_self_claims(
    tmp_path: Path,
    version: str,
    minimum: str,
) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    payload = _manifest().model_dump(mode="json")
    payload["officecli"]["version"] = version
    payload["officecli"]["minimum_version"] = minimum

    with pytest.raises(SetupError, match="OfficeCLI release policy"):
        manager.plan_install(ReleaseManifest.model_validate(payload), requested_targets=None)


def test_caller_cannot_bypass_health_with_forged_already_healthy(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    forged = replace(
        manager.plan_install(_manifest(), requested_targets=None),
        already_healthy=True,
    )

    result = manager.install(forged, consent=lambda _: False)

    assert result.status is SetupStatus.CANCELLED
    assert harness.effects == []


def test_runtime_launcher_tampering_is_unhealthy(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    launcher = plan.runtime_path / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"tampered")

    assert manager.doctor() is False


def test_missing_adapter_makes_doctor_unhealthy(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    (adapters[AssistantKind.CODEX].destination / "asset.bin").unlink()

    assert manager.doctor() is False


def test_repair_of_officecli_only_does_not_reinstall_runtime_or_adapters(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    plan.officecli_path.write_bytes(b"x" * plan.officecli_asset.size)
    harness.effects.clear()

    result = manager.repair(plan, consent=lambda _: True)

    assert result.status is SetupStatus.READY
    assert [name for name, _ in harness.effects if name in {"download", "runtime", "adapter"}] == [
        "download"
    ]
    assert all(adapter.calls == 1 for adapter in adapters.values())


def test_adapter_mutation_is_rolled_back_without_partial_state(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    adapters[AssistantKind.CLAUDE].fail = True
    plan = manager.plan_install(_manifest(), requested_targets=None)
    result = manager.install(plan, consent=lambda _: True)
    assert result.status is SetupStatus.FAILED
    assert result.activated is False
    assert result.error == "assistant adapter installation failed"
    assert not (tmp_path / "data" / "state.json").exists()
    assert all(not adapter.destination.exists() for adapter in adapters.values())


def test_uninstall_failure_returns_partial_and_keeps_failed_adapter_recorded(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    adapters[AssistantKind.CLAUDE].uninstall_failure = True

    result = manager.uninstall(consent=lambda: True)

    assert result.status is SetupStatus.PARTIAL
    assert result.error == "native adapter cleanup is incomplete"
    assert adapters[AssistantKind.CLAUDE].destination.exists()
    state = __import__("csaf.setup.assets", fromlist=["read_json"]).read_json(
        tmp_path / "data" / "state.json"
    )
    assert "claude" in state["adapter_targets"]


def test_dependency_setup_error_is_sanitized(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness, detected=(AssistantKind.CODEX,))
    adapters[AssistantKind.CODEX].fail = True

    result = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.error == "assistant adapter installation failed"
    assert "secret-value" not in result.error
    assert "Alice" not in result.error


def test_runtime_only_repair_does_not_reinstall_officecli_or_adapters(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    launcher = plan.runtime_path / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"damaged")
    harness.effects.clear()

    assert manager.repair(plan, consent=lambda _: True).status is SetupStatus.READY
    names = [name for name, _ in harness.effects if name in {"download", "runtime", "adapter"}]
    assert names == ["download", "runtime"]
    assert all(adapter.calls == 1 for adapter in adapters.values())


def test_adapter_only_repair_downloads_only_damaged_adapter(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    (adapters[AssistantKind.CODEX].destination / "asset.bin").unlink()
    harness.effects.clear()

    assert manager.repair(plan, consent=lambda _: True).status is SetupStatus.READY
    names = [name for name, _ in harness.effects if name in {"download", "runtime", "adapter"}]
    assert names == ["download", "adapter"]
    assert adapters[AssistantKind.CODEX].calls == 2
    assert adapters[AssistantKind.CLAUDE].calls == 1


def test_same_size_corrupt_adapter_download_fails_before_adapter_mutation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness, detected=(AssistantKind.CODEX,))
    original = harness.download

    def corrupt(asset: object, destination: Path) -> Path:
        value = original(asset, destination)
        if "codex" in str(getattr(asset, "url", "")):
            destination.write_bytes(b"wrong")
        return value

    manager._downloader = corrupt
    result = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status is SetupStatus.FAILED
    assert result.error == "verified asset failed manifest verification"
    assert adapters[AssistantKind.CODEX].calls == 0


def test_doctor_failure_after_all_adapters_rolls_them_back(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    harness.fail = "doctor"
    result = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )
    assert result.status is SetupStatus.FAILED
    assert result.error == "setup diagnostics failed"
    assert not (tmp_path / "data" / "current.json").exists()
    assert not (tmp_path / "data" / "state.json").exists()
    assert all(not adapter.destination.exists() for adapter in adapters.values())


def test_retry_after_rolled_back_adapter_failure_reinstalls_both(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    adapters[AssistantKind.CLAUDE].fail = True
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.FAILED
    adapters[AssistantKind.CLAUDE].fail = False
    harness.effects.clear()
    assert manager.repair(plan, consent=lambda _: True).status is SetupStatus.READY
    assert adapters[AssistantKind.CODEX].calls == 2
    assert adapters[AssistantKind.CLAUDE].calls == 2


def test_uninstall_retains_owned_officecli_without_include_flag(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY

    assert manager.uninstall(consent=lambda: True).status is SetupStatus.READY
    assert plan.officecli_path.is_file()
    state = __import__("csaf.setup.assets", fromlist=["read_json"]).read_json(
        tmp_path / "data" / "state.json"
    )
    assert state["officecli_installed_by_csaf"] is True
    assert state["active_version"] is None


def test_uninstall_removes_owned_officecli_only_with_include_flag(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY

    assert (
        manager.uninstall(consent=lambda: True, include_officecli=True).status is SetupStatus.READY
    )
    assert not plan.officecli_path.exists()
    assert not (tmp_path / "data" / "state.json").exists()


def test_partial_uninstall_clears_pointer_if_runtime_was_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    original = manager._remove_owned

    def fail_current(path: Path) -> None:
        if path.name == "current.json":
            raise SetupError("token=secret-value C:\\Users\\Alice\\current.json")
        original(path)

    monkeypatch.setattr(manager, "_remove_owned", fail_current)

    result = manager.uninstall(consent=lambda: True)

    assert result.status is SetupStatus.PARTIAL
    assert result.activated is False
    assert not (tmp_path / "data" / "current.json").exists()
    assert "secret-value" not in result.error


def test_consent_dependency_error_is_sanitized_without_material_effect(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())

    def fail_consent(_plan: object) -> bool:
        raise SetupError("token=secret-value C:\\Users\\Alice\\consent.log")

    result = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=fail_consent,
    )

    assert result.status is SetupStatus.FAILED
    assert result.error == "setup consent failed"
    assert harness.effects == []
    assert not (tmp_path / "data").exists()


def test_state_failure_rolls_back_changed_adapters(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    harness.fail = "state"
    result = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )
    assert result.status is SetupStatus.FAILED
    assert all(not adapter.destination.exists() for adapter in adapters.values())
    assert ("adapter-uninstall", AssistantKind.CODEX) in harness.effects
    assert ("adapter-uninstall", AssistantKind.CLAUDE) in harness.effects


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_adapter_only_repair_doctor_failure_preserves_runtime_office_and_current(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    (adapters[AssistantKind.CODEX].destination / "asset.bin").unlink()
    runtime_before = _tree_bytes(plan.runtime_path)
    office_before = plan.officecli_path.read_bytes()
    current_before = (tmp_path / "data" / "current.json").read_bytes()
    harness.fail = "doctor"
    result = manager.repair(plan, consent=lambda _: True)
    assert result.status is SetupStatus.FAILED
    assert _tree_bytes(plan.runtime_path) == runtime_before
    assert plan.officecli_path.read_bytes() == office_before
    assert (tmp_path / "data" / "current.json").read_bytes() == current_before


def test_runtime_only_repair_doctor_failure_preserves_untouched_components(
    tmp_path: Path,
) -> None:
    from csaf.setup.assets import read_json, write_json_atomic

    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness)
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    state_path = tmp_path / "data" / "state.json"
    state = read_json(state_path)
    state["verified_checksums"]["runtime-content:0.1.0"] = "0" * 64
    write_json_atomic(state_path, state)
    runtime_before = _tree_bytes(plan.runtime_path)
    office_before = plan.officecli_path.read_bytes()
    current_before = (tmp_path / "data" / "current.json").read_bytes()
    harness.fail = "doctor"

    result = manager.repair(plan, consent=lambda _: True)

    assert result.status is SetupStatus.FAILED
    assert _tree_bytes(plan.runtime_path) == runtime_before
    assert plan.officecli_path.read_bytes() == office_before
    assert (tmp_path / "data" / "current.json").read_bytes() == current_before


def test_failed_adapter_update_restores_active_runtime_adapter_versions(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness)
    first = manager.plan_install(_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    harness.fail = "doctor"
    result = manager.install(
        manager.plan_install(_manifest("0.2.0"), requested_targets=None),
        consent=lambda _: True,
    )
    assert result.status is SetupStatus.FAILED
    harness.fail = None
    assert manager.doctor() is True
    assert manager._active_version() == first.manifest.version


class ClaudeLifecycleRunner:
    def __init__(self) -> None:
        self.marketplace_ref: str | None = None
        self.plugin = False
        self.calls: list[list[str]] = []
        self.fail_new_plugin_install = False
        self.fail_marketplace_remove = False

    @property
    def marketplace(self) -> bool:
        return self.marketplace_ref is not None

    @marketplace.setter
    def marketplace(self, value: bool) -> None:
        self.marketplace_ref = "v0.1.0" if value else None

    def __call__(
        self, command: Sequence[str], *, stdout: BinaryIO, stderr: BinaryIO, timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        del timeout
        call = list(command)
        self.calls.append(call)
        if call[2:5] == ["marketplace", "list", "--json"]:
            payload = (
                [
                    {
                        "name": "csaf",
                        "url": "https://github.com/karthiknambiar/csm-skills-framework.git",
                        "ref": self.marketplace_ref,
                        "source": "git",
                    }
                ]
                if self.marketplace_ref is not None
                else []
            )
            stdout.write(json.dumps(payload).encode())
        elif call[2:4] == ["list", "--json"]:
            stdout.write(
                json.dumps([{"name": "csaf@csaf", "scope": "user"}] if self.plugin else []).encode()
            )
        elif call[2:4] == ["marketplace", "add"]:
            self.marketplace_ref = call[-1].rsplit("#", 1)[-1]
        elif call[2:4] == ["install", "csaf@csaf"]:
            if self.fail_new_plugin_install and self.marketplace_ref == "v0.2.0":
                self.fail_new_plugin_install = False
                stderr.write(b"token=secret-value C:\\Users\\Alice\\claude.log")
                return subprocess.CompletedProcess(call, 7)
            self.plugin = True
        elif call[2:4] == ["uninstall", "csaf@csaf"]:
            self.plugin = False
        elif call[2:4] == ["marketplace", "remove"]:
            if self.fail_marketplace_remove:
                self.fail_marketplace_remove = False
                stderr.write(b"token=secret-value C:\\Users\\Alice\\claude.log")
                return subprocess.CompletedProcess(call, 8)
            self.marketplace_ref = None
        return subprocess.CompletedProcess(call, 0)


def test_actual_codex_installer_lifecycle_consumes_verified_archive(tmp_path: Path) -> None:
    archive = tmp_path / "codex.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("SKILL.md", "# CSAF native")
        bundle.writestr("scripts/launch", "#!/bin/sh\n")
    codex_bytes = archive.read_bytes()
    payload = _manifest().model_dump(mode="json")
    payload["codex_skill"] = _asset(codex_bytes, "codex.zip")
    manifest = ReleaseManifest.model_validate(payload)
    harness = Harness(tmp_path)
    original_download = harness.download

    def download(asset: object, destination: Path) -> Path:
        if "codex.zip" in str(getattr(asset, "url", "")):
            harness.effects.append(("download", asset))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(codex_bytes)
            return destination
        return original_download(asset, destination)

    managed = CodexManagedAdapter(tmp_path / "codex" / "skills")
    manager = SetupManager(
        data_root=tmp_path / "data",
        platform=PLATFORM,
        detected_assistants=(AssistantKind.CODEX,),
        downloader=download,
        runtime_installer=harness.install_runtime,
        adapter_installers={AssistantKind.CODEX: managed},
        doctor_runner=harness.doctor,
        runtime_probe=harness.runtime_probe,
        officecli_probe=harness.officecli_probe,
    )
    plan = manager.plan_install(manifest, requested_targets=None)
    assert plan.adapter_destinations[AssistantKind.CODEX] == managed.destination

    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    assert (managed.destination / "SKILL.md").read_text(encoding="utf-8") == "# CSAF native"
    receipt = json.loads((managed.destination / ".csaf-adapter.json").read_text(encoding="utf-8"))
    assert receipt["asset_sha256"] == hashlib.sha256(codex_bytes).hexdigest()
    assert manager.doctor() is True
    (managed.destination / "SKILL.md").write_text("damaged", encoding="utf-8")
    assert manager.repair(plan, consent=lambda _: True).status is SetupStatus.READY
    assert (managed.destination / "SKILL.md").read_text(encoding="utf-8") == "# CSAF native"
    assert manager.uninstall(consent=lambda: True).status is SetupStatus.READY
    assert not managed.destination.exists()


def test_actual_gemini_installer_lifecycle_records_repairs_and_uninstalls(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "canonical-skill.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("SKILL.md", "# CSAF native")
        bundle.writestr("scripts/launch", "#!/bin/sh\n")
    skill_bytes = archive.read_bytes()
    payload = _manifest().model_dump(mode="json")
    payload["codex_skill"] = _asset(skill_bytes, "canonical-skill.zip")
    manifest = ReleaseManifest.model_validate(payload)
    harness = Harness(tmp_path)
    original_download = harness.download

    def download(asset: object, destination: Path) -> Path:
        if "canonical-skill.zip" in str(getattr(asset, "url", "")):
            harness.effects.append(("download", asset))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(skill_bytes)
            return destination
        return original_download(asset, destination)

    managed = GeminiManagedAdapter(tmp_path / ".gemini" / "skills")
    manager = SetupManager(
        data_root=tmp_path / "data",
        platform=PLATFORM,
        detected_assistants=(AssistantKind.GEMINI,),
        downloader=download,
        runtime_installer=harness.install_runtime,
        adapter_installers={AssistantKind.GEMINI: managed},
        doctor_runner=harness.doctor,
        runtime_probe=harness.runtime_probe,
        officecli_probe=harness.officecli_probe,
    )
    plan = manager.plan_install(manifest, requested_targets=None)

    assert plan.targets == (AssistantKind.GEMINI,)
    assert plan.adapter_assets[AssistantKind.GEMINI] == manifest.codex_skill
    assert plan.adapter_destinations[AssistantKind.GEMINI] == managed.destination
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    state = json.loads((tmp_path / "data" / "state.json").read_text(encoding="utf-8"))
    assert state["adapter_targets"] == {"gemini": str(managed.destination)}
    assert manager.doctor() is True

    (managed.destination / "SKILL.md").write_text("damaged", encoding="utf-8")
    assert manager.repair(plan, consent=lambda _: True).status is SetupStatus.READY
    assert (managed.destination / "SKILL.md").read_text(encoding="utf-8") == "# CSAF native"
    assert manager.uninstall(consent=lambda: True).status is SetupStatus.READY
    assert not managed.destination.exists()


def test_actual_claude_installer_lifecycle_preserves_exact_remote_pin(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    runner = ClaudeLifecycleRunner()
    managed = ClaudeManagedAdapter(tmp_path / "data" / "adapters" / "claude", runner=runner)
    manager = SetupManager(
        data_root=tmp_path / "data",
        platform=PLATFORM,
        detected_assistants=(AssistantKind.CLAUDE,),
        downloader=harness.download,
        runtime_installer=harness.install_runtime,
        adapter_installers={AssistantKind.CLAUDE: managed},
        doctor_runner=harness.doctor,
        runtime_probe=harness.runtime_probe,
        officecli_probe=harness.officecli_probe,
    )
    plan = manager.plan_install(_manifest(), requested_targets=None)

    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    add = next(call for call in runner.calls if call[2:4] == ["marketplace", "add"])
    assert add[-1].endswith("#v0.1.0")
    receipt = json.loads((managed.destination / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["asset_sha256"] == plan.adapter_assets[AssistantKind.CLAUDE].sha256
    assert manager.doctor() is True
    runner.plugin = False
    assert manager.repair(plan, consent=lambda _: True).status is SetupStatus.READY
    assert manager.uninstall(consent=lambda: True).status is SetupStatus.READY
    assert runner.plugin is False
    assert runner.marketplace is False


def test_claude_facade_does_not_remove_preexisting_marketplace(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    runner = ClaudeLifecycleRunner()
    runner.marketplace = True
    managed = ClaudeManagedAdapter(tmp_path / "data" / "adapters" / "claude", runner=runner)
    manager = SetupManager(
        data_root=tmp_path / "data",
        platform=PLATFORM,
        detected_assistants=(AssistantKind.CLAUDE,),
        downloader=harness.download,
        runtime_installer=harness.install_runtime,
        adapter_installers={AssistantKind.CLAUDE: managed},
        doctor_runner=harness.doctor,
        runtime_probe=harness.runtime_probe,
        officecli_probe=harness.officecli_probe,
    )
    plan = manager.plan_install(_manifest(), requested_targets=None)

    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    receipt = json.loads((managed.destination / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["marketplace_owned"] is False
    assert receipt["plugin_owned"] is True
    assert manager.uninstall(consent=lambda: True).status is SetupStatus.READY
    assert runner.plugin is False
    assert runner.marketplace is True


def _claude_manifest(version: str, plugin: bytes) -> ReleaseManifest:
    payload = _manifest(version).model_dump(mode="json")
    payload["claude_plugin"] = _asset(plugin, f"claude-{version}.zip")
    return ReleaseManifest.model_validate(payload)


def _claude_update_manager(
    tmp_path: Path,
    harness: Harness,
    runner: ClaudeLifecycleRunner,
) -> tuple[SetupManager, ClaudeManagedAdapter]:
    plugin_payloads = {
        "claude-0.1.0.zip": b"claude-v1",
        "claude-0.2.0.zip": b"claude-v2",
    }
    original = harness.download

    def download(asset: object, destination: Path) -> Path:
        url = str(getattr(asset, "url", ""))
        for name, payload in plugin_payloads.items():
            if name in url:
                harness.effects.append(("download", asset))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
                return destination
        return original(asset, destination)

    managed = ClaudeManagedAdapter(tmp_path / "data" / "adapters" / "claude", runner=runner)
    manager = SetupManager(
        data_root=tmp_path / "data",
        platform=PLATFORM,
        detected_assistants=(AssistantKind.CLAUDE,),
        downloader=download,
        runtime_installer=harness.install_runtime,
        adapter_installers={AssistantKind.CLAUDE: managed},
        doctor_runner=harness.doctor,
        runtime_probe=harness.runtime_probe,
        officecli_probe=harness.officecli_probe,
    )
    return manager, managed


def test_owned_claude_adapter_transitions_from_v1_to_v2(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    runner = ClaudeLifecycleRunner()
    manager, managed = _claude_update_manager(tmp_path, harness, runner)
    first = manager.plan_install(_claude_manifest("0.1.0", b"claude-v1"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    transition_start = len(runner.calls)
    second = manager.plan_install(_claude_manifest("0.2.0", b"claude-v2"), requested_targets=None)

    result = manager.install(second, consent=lambda _: True)

    assert result.status is SetupStatus.READY
    assert manager._active_version() == second.manifest.version
    assert runner.marketplace_ref == "v0.2.0"
    assert runner.plugin is True
    receipt = json.loads((managed.destination / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["version"] == "0.2.0"
    assert receipt["asset_sha256"] == second.adapter_assets[AssistantKind.CLAUDE].sha256
    assert receipt["marketplace_owned"] is True
    assert receipt["plugin_owned"] is True
    operations = [call[2:] for call in runner.calls[transition_start:]]
    uninstall_index = operations.index(["uninstall", "csaf@csaf", "--scope", "user"])
    remove_index = operations.index(["marketplace", "remove", "csaf"])
    add_index = next(i for i, call in enumerate(operations) if call[:2] == ["marketplace", "add"])
    install_index = operations.index(["install", "csaf@csaf", "--scope", "user"])
    assert uninstall_index < remove_index < add_index < install_index


def test_owned_claude_transition_failure_restores_v1_and_current(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    runner = ClaudeLifecycleRunner()
    manager, managed = _claude_update_manager(tmp_path, harness, runner)
    first = manager.plan_install(_claude_manifest("0.1.0", b"claude-v1"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    receipt_before = (managed.destination / "receipt.json").read_bytes()
    current_before = (tmp_path / "data" / "current.json").read_bytes()
    runtime_before = _tree_bytes(first.runtime_path)
    runner.fail_new_plugin_install = True

    result = manager.install(
        manager.plan_install(_claude_manifest("0.2.0", b"claude-v2"), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status is SetupStatus.FAILED
    assert result.activated is False
    assert runner.marketplace_ref == "v0.1.0"
    assert runner.plugin is True
    assert (managed.destination / "receipt.json").read_bytes() == receipt_before
    assert (tmp_path / "data" / "current.json").read_bytes() == current_before
    assert _tree_bytes(first.runtime_path) == runtime_before
    assert manager.doctor() is True
    assert "secret-value" not in result.error
    assert "Alice" not in result.error


def test_unowned_claude_version_conflict_fails_before_mutation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    runner = ClaudeLifecycleRunner()
    runner.marketplace_ref = "v0.1.0"
    runner.plugin = True
    manager, managed = _claude_update_manager(tmp_path, harness, runner)
    first = manager.plan_install(_claude_manifest("0.1.0", b"claude-v1"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    receipt_before = (managed.destination / "receipt.json").read_bytes()
    current_before = (tmp_path / "data" / "current.json").read_bytes()
    transition_start = len(runner.calls)

    result = manager.install(
        manager.plan_install(_claude_manifest("0.2.0", b"claude-v2"), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status is SetupStatus.FAILED
    assert result.error == "Claude adapter version conflict is not CSAF-owned"
    assert runner.marketplace_ref == "v0.1.0"
    assert runner.plugin is True
    assert (managed.destination / "receipt.json").read_bytes() == receipt_before
    assert (tmp_path / "data" / "current.json").read_bytes() == current_before
    operations = [call[2:] for call in runner.calls[transition_start:]]
    assert not any(call and call[0] in {"uninstall"} for call in operations)
    assert ["marketplace", "remove", "csaf"] not in operations


def test_held_setup_lock_preserves_live_transaction_and_blocks_install(tmp_path: Path) -> None:
    from csaf.setup.assets import _acquire_activation_lock, _release_activation_lock

    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    data = tmp_path / "data"
    live = data / ".staging" / "live-transaction"
    live.mkdir(parents=True)
    sentinel = live / "sentinel"
    sentinel.write_bytes(b"live")
    descriptor = _acquire_activation_lock(data / ".setup.lock")
    try:
        result = manager.install(
            manager.plan_install(_manifest(), requested_targets=None),
            consent=lambda _: True,
        )
    finally:
        _release_activation_lock(descriptor)

    assert result.status is SetupStatus.FAILED
    assert result.error == "another setup operation is already in progress"
    assert sentinel.read_bytes() == b"live"
    assert harness.effects == []


def test_explicit_update_subset_rejected_before_mutation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    first = manager.plan_install(_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    effects_before = list(harness.effects)
    calls_before = {kind: adapter.calls for kind, adapter in adapters.items()}

    with pytest.raises(SetupError, match="must include every installed assistant"):
        manager.plan_install(_manifest("0.2.0"), requested_targets=(AssistantKind.CODEX,))

    assert harness.effects == effects_before
    assert {kind: adapter.calls for kind, adapter in adapters.items()} == calls_before
    assert manager.doctor() is True


def test_failed_v2_doctor_restores_both_v1_adapters_and_ready_doctor(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    first = manager.plan_install(_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    state_before = (tmp_path / "data" / "state.json").read_bytes()
    current_before = (tmp_path / "data" / "current.json").read_bytes()
    adapter_before = {kind: _tree_bytes(adapter.destination) for kind, adapter in adapters.items()}
    harness.fail = "doctor"

    result = manager.install(
        manager.plan_install(_manifest("0.2.0"), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status is SetupStatus.FAILED
    assert result.activated is False
    assert (tmp_path / "data" / "state.json").read_bytes() == state_before
    assert (tmp_path / "data" / "current.json").read_bytes() == current_before
    assert {
        kind: _tree_bytes(adapter.destination) for kind, adapter in adapters.items()
    } == adapter_before
    harness.fail = None
    assert manager.doctor() is True


def test_uninstall_runtime_failure_keeps_state_in_sync_with_removed_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from csaf.setup.assets import read_json

    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    original = manager._remove_owned

    def fail_versions(path: Path) -> None:
        if path.name == "versions":
            raise SetupError("runtime cleanup failed")
        original(path)

    monkeypatch.setattr(manager, "_remove_owned", fail_versions)
    result = manager.uninstall(consent=lambda: True)

    assert result.status is SetupStatus.PARTIAL
    assert all(not adapter.destination.exists() for adapter in adapters.values())
    state = read_json(tmp_path / "data" / "state.json")
    assert state["adapter_targets"] == {}
    assert not any(key.startswith("adapter:") for key in state["verified_checksums"])


def test_state_restore_uncertainty_is_reported_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from csaf.setup.assets import write_json_atomic as real_write

    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness)
    first = manager.plan_install(_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    harness.fail = "state"

    def fail_restore(path: Path, value: object) -> None:
        if path.name == "state.json":
            raise SetupError("durability uncertain")
        real_write(path, value)

    monkeypatch.setattr("csaf.setup.manager.write_json_atomic", fail_restore)
    result = manager.install(
        manager.plan_install(_manifest("0.2.0"), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status is SetupStatus.PARTIAL
    assert result.error == "installation rollback is incomplete"


def test_mocked_reparse_parent_blocks_activation_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    versions = tmp_path / "data" / "versions"
    versions.mkdir(parents=True)
    sentinel = versions / "sentinel"
    sentinel.write_bytes(b"safe")
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"new")
    original_lstat = Path.lstat

    class ReparseDetails:
        def __init__(self, details: os.stat_result) -> None:
            self._details = details
            self.st_mode = details.st_mode
            self.st_file_attributes = 0x400

        def __getattr__(self, name: str) -> object:
            return getattr(self._details, name)

    def mocked_lstat(path: Path) -> os.stat_result:
        details = original_lstat(path)
        if path == versions:
            return ReparseDetails(details)  # type: ignore[return-value]
        return details

    monkeypatch.setattr(Path, "lstat", mocked_lstat)
    with pytest.raises(SetupError, match="symlink|reparse"):
        manager._activate(source, versions / "0.2.0", tmp_path / "backup")

    assert sentinel.read_bytes() == b"safe"
    assert (source / "payload").read_bytes() == b"new"
    assert not (versions / "0.2.0").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink boundary")
def test_posix_symlinked_versions_parent_never_writes_outside(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    data = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside")
    data.mkdir()
    (data / "versions").symlink_to(outside, target_is_directory=True)

    result = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status in {SetupStatus.FAILED, SetupStatus.PARTIAL}
    assert sentinel.read_bytes() == b"outside"
    assert not (outside / "0.1.0").exists()


def test_setup_lock_recovers_after_subprocess_crash(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    data = tmp_path / "data"
    data.mkdir()
    _secure_posix_fixture(data)
    ready = tmp_path / "ready"
    script = (
        "import time; from pathlib import Path; "
        "from csaf.setup.assets import _acquire_activation_lock; "
        f"fd=_acquire_activation_lock(Path({str(data / '.setup.lock')!r})); "
        f"Path({str(ready)!r}).write_text('ready'); time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        for _ in range(250):
            if ready.exists() or process.poll() is not None:
                break
            time.sleep(0.02)
        assert ready.exists(), "setup lock subprocess failed to become ready"
        blocked = manager.install(
            manager.plan_install(_manifest(), requested_targets=None),
            consent=lambda _: True,
        )
        assert blocked.status is SetupStatus.FAILED
    finally:
        process.kill()
        process.wait(timeout=10)

    recovered = manager.install(
        manager.plan_install(_manifest(), requested_targets=None),
        consent=lambda _: True,
    )
    assert recovered.status is SetupStatus.READY


def test_doctor_recovers_interrupted_runtime_backup_before_stale_cleanup(tmp_path: Path) -> None:
    from csaf.setup.assets import read_json, write_json_atomic

    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness, detected=())
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    state_before = read_json(tmp_path / "data" / "state.json")
    current_before = read_json(tmp_path / "data" / "current.json")
    stale = tmp_path / "data" / ".staging" / "crashed"
    stale.mkdir(parents=True)
    _secure_posix_fixture(stale)
    backup = stale / "previous-runtime"
    os.replace(plan.runtime_path, backup)
    write_json_atomic(stale / "previous-state.json", state_before)
    write_json_atomic(stale / "previous-current.json", current_before)
    write_json_atomic(
        stale / "transaction.json",
        {
            "schema_version": 1,
            "version": "0.2.0",
            "runtime_path": str(plan.runtime_path),
            "officecli_path": str(plan.officecli_path),
            "state_present": True,
            "current_present": True,
        },
    )

    assert manager.doctor() is True
    assert plan.runtime_path.is_dir()
    assert not stale.exists()


def test_actual_claude_downstream_failure_restores_owned_v1(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    runner = ClaudeLifecycleRunner()
    manager, managed = _claude_update_manager(tmp_path, harness, runner)
    first = manager.plan_install(_claude_manifest("0.1.0", b"claude-v1"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    receipt_before = (managed.destination / "receipt.json").read_bytes()
    current_before = (tmp_path / "data" / "current.json").read_bytes()
    harness.fail = "doctor"

    result = manager.install(
        manager.plan_install(_claude_manifest("0.2.0", b"claude-v2"), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status is SetupStatus.FAILED
    assert runner.marketplace_ref == "v0.1.0"
    assert runner.plugin is True
    assert (managed.destination / "receipt.json").read_bytes() == receipt_before
    assert (tmp_path / "data" / "current.json").read_bytes() == current_before
    harness.fail = None
    assert manager.doctor() is True


def test_actual_codex_downstream_failure_restores_v1_tree(tmp_path: Path) -> None:
    archives: dict[str, bytes] = {}
    for version, body in (("0.1.0", "# CSAF v1"), ("0.2.0", "# CSAF v2")):
        archive = tmp_path / f"codex-{version}.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("SKILL.md", body)
            bundle.writestr("scripts/launch", "#!/bin/sh\n")
        archives[version] = archive.read_bytes()

    def manifest(version: str) -> ReleaseManifest:
        payload = _manifest(version).model_dump(mode="json")
        payload["codex_skill"] = _asset(archives[version], f"codex-{version}.zip")
        return ReleaseManifest.model_validate(payload)

    harness = Harness(tmp_path)
    original_download = harness.download

    def download(asset: object, destination: Path) -> Path:
        url = str(getattr(asset, "url", ""))
        for version, payload in archives.items():
            if f"codex-{version}.zip" in url:
                harness.effects.append(("download", asset))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
                return destination
        return original_download(asset, destination)

    managed = CodexManagedAdapter(tmp_path / "codex" / "skills")
    manager = SetupManager(
        data_root=tmp_path / "data",
        platform=PLATFORM,
        detected_assistants=(AssistantKind.CODEX,),
        downloader=download,
        runtime_installer=harness.install_runtime,
        adapter_installers={AssistantKind.CODEX: managed},
        doctor_runner=harness.doctor,
        runtime_probe=harness.runtime_probe,
        officecli_probe=harness.officecli_probe,
    )
    first = manager.plan_install(manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    tree_before = _tree_bytes(managed.destination)
    current_before = (tmp_path / "data" / "current.json").read_bytes()
    harness.fail = "doctor"

    result = manager.install(
        manager.plan_install(manifest("0.2.0"), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status is SetupStatus.FAILED
    assert _tree_bytes(managed.destination) == tree_before
    assert (tmp_path / "data" / "current.json").read_bytes() == current_before
    harness.fail = None
    assert manager.doctor() is True


def test_claude_partial_uninstall_receipt_supports_safe_retry(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    runner = ClaudeLifecycleRunner()
    managed = ClaudeManagedAdapter(tmp_path / "data" / "adapters" / "claude", runner=runner)
    manager = SetupManager(
        data_root=tmp_path / "data",
        platform=PLATFORM,
        detected_assistants=(AssistantKind.CLAUDE,),
        downloader=harness.download,
        runtime_installer=harness.install_runtime,
        adapter_installers={AssistantKind.CLAUDE: managed},
        doctor_runner=harness.doctor,
        runtime_probe=harness.runtime_probe,
        officecli_probe=harness.officecli_probe,
    )
    plan = manager.plan_install(_manifest(), requested_targets=None)
    assert manager.install(plan, consent=lambda _: True).status is SetupStatus.READY
    runner.fail_marketplace_remove = True

    first = manager.uninstall(consent=lambda: True)

    assert first.status is SetupStatus.PARTIAL
    receipt = json.loads((managed.destination / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["plugin_owned"] is False
    assert receipt["marketplace_owned"] is True
    assert runner.plugin is False
    assert runner.marketplace_ref == "v0.1.0"
    retry_start = len(runner.calls)

    second = manager.uninstall(consent=lambda: True)

    assert second.status is SetupStatus.READY
    retry_calls = [call[2:] for call in runner.calls[retry_start:]]
    assert ["uninstall", "csaf@csaf", "--scope", "user"] not in retry_calls
    assert ["marketplace", "remove", "csaf"] in retry_calls
    assert not managed.destination.exists()


class PersistedClaudeRunner(ClaudeLifecycleRunner):
    def __init__(self, state_path: Path) -> None:
        super().__init__()
        self.state_path = state_path
        self._load()

    def _load(self) -> None:
        if self.state_path.is_file():
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.marketplace_ref = value["marketplace_ref"]
            self.plugin = value["plugin"]

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {"marketplace_ref": self.marketplace_ref, "plugin": self.plugin},
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def __call__(
        self, command: Sequence[str], *, stdout: BinaryIO, stderr: BinaryIO, timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        self._load()
        result = super().__call__(command, stdout=stdout, stderr=stderr, timeout=timeout)
        self._save()
        return result


def _deterministic_codex_archive(version: str) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, body in (
            ("SKILL.md", f"# CSAF {version}"),
            ("scripts/launch", "#!/bin/sh\n"),
        ):
            member = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            bundle.writestr(member, body)
    return archive.getvalue()


def _actual_crash_manager(
    root: Path,
    *,
    checkpoint: Any = None,
) -> tuple[SetupManager, CodexManagedAdapter, ClaudeManagedAdapter, PersistedClaudeRunner]:
    harness = Harness(root)
    adapter_root = root / "data" / "adapters"
    adapter_root.mkdir(parents=True, exist_ok=True)
    _secure_posix_fixture(root / "data")
    _secure_posix_fixture(adapter_root)
    original_download = harness.download

    def download(asset: object, destination: Path) -> Path:
        url = str(getattr(asset, "url", ""))
        if "codex-" in url:
            version = url.rsplit("codex-", 1)[-1].removesuffix(".zip")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_deterministic_codex_archive(version))
            return destination
        if "claude-" in url:
            version = url.rsplit("claude-", 1)[-1].removesuffix(".zip")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(f"claude-{version}".encode())
            return destination
        return original_download(asset, destination)

    runner = PersistedClaudeRunner(root / "external" / "claude.json")
    codex = CodexManagedAdapter(root / "data" / "adapters" / "codex", checkpoint=checkpoint)
    claude = ClaudeManagedAdapter(
        root / "data" / "adapters" / "claude", runner=runner, checkpoint=checkpoint
    )
    manager = SetupManager(
        data_root=root / "data",
        platform=PLATFORM,
        detected_assistants=(AssistantKind.CODEX, AssistantKind.CLAUDE),
        downloader=download,
        runtime_installer=harness.install_runtime,
        adapter_installers={AssistantKind.CODEX: codex, AssistantKind.CLAUDE: claude},
        doctor_runner=harness.doctor,
        runtime_probe=harness.runtime_probe,
        officecli_probe=harness.officecli_probe,
        checkpoint=checkpoint,
    )
    return manager, codex, claude, runner


def _actual_crash_manifest(version: str) -> ReleaseManifest:
    payload = _manifest(version).model_dump(mode="json")
    codex = _deterministic_codex_archive(version)
    payload["codex_skill"] = _asset(codex, f"codex-{version}.zip")
    payload["claude_plugin"] = _asset(f"claude-{version}".encode(), f"claude-{version}.zip")
    return ReleaseManifest.model_validate(payload)


def _crash_install_subprocess(tmp_path: Path, phase: str, version: str) -> int:
    script = (
        "import os, runpy; from pathlib import Path; "
        f"ns=runpy.run_path({str(Path(__file__))!r}); "
        f"root=Path({str(tmp_path)!r}); "
        "h=ns['Harness'](root); m,_=ns['_manager'](root,h); "
        f"m._checkpoint=lambda value: os._exit(71) if value == {phase!r} else None; "
        f"p=m.plan_install(ns['_manifest']({version!r}), requested_targets=None); "
        "m.install(p, consent=lambda _: True, assume_yes=True)"
    )
    return subprocess.run([sys.executable, "-c", script], check=False).returncode


def _actual_crash_subprocess(tmp_path: Path, phase: str, version: str) -> int:
    script = (
        "import os, runpy; from pathlib import Path; "
        f"ns=runpy.run_path({str(Path(__file__))!r}); "
        f"root=Path({str(tmp_path)!r}); "
        f"checkpoint=lambda value: os._exit(71) if value == {phase!r} else None; "
        "m,*_=ns['_actual_crash_manager'](root,checkpoint=checkpoint); "
        f"p=m.plan_install(ns['_actual_crash_manifest']({version!r}), requested_targets=None); "
        "m.install(p, consent=lambda _: True, assume_yes=True)"
    )
    return subprocess.run([sys.executable, "-c", script], check=False).returncode


def _actual_recovery_subprocess(tmp_path: Path, phase: str) -> int:
    script = (
        "import os, runpy; from pathlib import Path; "
        f"ns=runpy.run_path({str(Path(__file__))!r}); "
        f"root=Path({str(tmp_path)!r}); "
        f"checkpoint=lambda value: os._exit(72) if value == {phase!r} else None; "
        "m,*_=ns['_actual_crash_manager'](root,checkpoint=checkpoint); m.doctor()"
    )
    return subprocess.run([sys.executable, "-c", script], check=False).returncode


def _actual_uninstall_subprocess(tmp_path: Path, phase: str) -> int:
    script = (
        "import os, runpy; from pathlib import Path; "
        f"ns=runpy.run_path({str(Path(__file__))!r}); "
        f"root=Path({str(tmp_path)!r}); "
        f"checkpoint=lambda value: os._exit(73) if value == {phase!r} else None; "
        "m,*_=ns['_actual_crash_manager'](root,checkpoint=checkpoint); "
        "m.uninstall(consent=lambda: True, assume_yes=True)"
    )
    return subprocess.run([sys.executable, "-c", script], check=False).returncode


def _codex_internal_backup_crash_subprocess(tmp_path: Path, version: str) -> int:
    script = (
        "import os, runpy; from pathlib import Path; "
        f"ns=runpy.run_path({str(Path(__file__))!r}); "
        f"root=Path({str(tmp_path)!r}); "
        "target=root/'data'/'adapters'/'codex'/'csaf'; "
        "backup=target.parent/'.csaf.backup'; real_replace=os.replace\n"
        "def stop(source,destination):\n"
        " real_replace(source,destination)\n"
        " if Path(source)==target and Path(destination)==backup: os._exit(71)\n"
        "os.replace=stop; m,*_=ns['_actual_crash_manager'](root); "
        f"p=m.plan_install(ns['_actual_crash_manifest']({version!r}), requested_targets=None); "
        "m.install(p, consent=lambda _: True, assume_yes=True)"
    )
    return subprocess.run([sys.executable, "-c", script], check=False).returncode


@pytest.mark.parametrize("phase", ["adapter:codex:mutated", "current-replaced"])
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_base_exception_retains_transaction_for_next_lifecycle_recovery(
    tmp_path: Path,
    phase: str,
    interruption: type[BaseException],
) -> None:
    harness = Harness(tmp_path)
    manager, _adapters = _manager(tmp_path, harness)
    first = manager.plan_install(_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY

    def interrupt(value: str) -> None:
        if value == phase:
            raise interruption()

    manager._checkpoint = interrupt
    second = manager.plan_install(_manifest("0.2.0"), requested_targets=None)
    with pytest.raises(interruption):
        manager.install(second, consent=lambda _: True)
    assert any((tmp_path / "data" / ".staging").iterdir())

    manager._checkpoint = lambda _phase: None
    assert manager.doctor() is True
    assert manager._active_version() == first.manifest.version
    assert not any((tmp_path / "data" / ".staging").iterdir())


@pytest.mark.parametrize("phase", ["adapter:codex:mutated", "current-replaced"])
def test_normal_checkpoint_exception_returns_sanitized_failure_after_cleanup(
    tmp_path: Path, phase: str
) -> None:
    harness = Harness(tmp_path)
    manager, _adapters = _manager(tmp_path, harness)
    first = manager.plan_install(_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY

    def fail(value: str) -> None:
        if value == phase:
            raise RuntimeError("token=private-value C:\\Users\\Alice\\checkpoint.log")

    manager._checkpoint = fail
    result = manager.install(
        manager.plan_install(_manifest("0.2.0"), requested_targets=None),
        consent=lambda _: True,
    )

    assert result.status is SetupStatus.FAILED
    assert result.error in {"native setup failed", "could not activate installation state"}
    assert "private-value" not in str(result)
    assert not any((tmp_path / "data" / ".staging").iterdir())
    manager._checkpoint = lambda _phase: None
    assert manager.doctor() is True


def test_actual_claude_uninstall_crash_after_receipt_deletion_retries_to_ready(
    tmp_path: Path,
) -> None:
    manager, _codex, claude, runner = _actual_crash_manager(tmp_path)
    first = manager.plan_install(_actual_crash_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY

    assert _actual_uninstall_subprocess(tmp_path, "claude:receipt-deleted") == 73
    assert not claude.destination.exists()
    assert (claude.destination.parent / ".claude-uninstall.json").is_file()

    result = manager.uninstall(consent=lambda: True)
    assert result.status is SetupStatus.READY
    runner._load()
    assert runner.plugin is False
    assert runner.marketplace_ref is None
    assert not (claude.destination.parent / ".claude-uninstall.json").exists()


def test_claude_uninstall_crash_preserves_preexisting_unowned_marketplace(
    tmp_path: Path,
) -> None:
    manager, _codex, claude, runner = _actual_crash_manager(tmp_path)
    runner.marketplace_ref = "v0.1.0"
    runner._save()
    first = manager.plan_install(_actual_crash_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY

    assert _actual_uninstall_subprocess(tmp_path, "claude:receipt-deleted") == 73
    runner._load()
    assert runner.marketplace_ref == "v0.1.0"
    assert runner.plugin is False

    assert manager.uninstall(consent=lambda: True).status is SetupStatus.READY
    runner._load()
    assert runner.marketplace_ref == "v0.1.0"
    assert runner.plugin is False
    assert not (claude.destination.parent / ".claude-uninstall.json").exists()


def test_actual_claude_uninstall_checkpoint_failure_retries_without_duplicate_mutation(
    tmp_path: Path,
) -> None:
    manager, _codex, claude, runner = _actual_crash_manager(tmp_path)
    first = manager.plan_install(_actual_crash_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    original_writer = claude._journal_writer

    def fail_after_plugin(path: Path, value: object) -> None:
        if isinstance(value, dict) and value.get("plugin_owned") is False:
            raise SetupError("token=private-value C:\\Users\\Alice\\journal.log")
        original_writer(path, value)

    claude._journal_writer = fail_after_plugin
    first_result = manager.uninstall(consent=lambda: True)
    assert first_result.status is SetupStatus.PARTIAL
    runner._load()
    assert runner.plugin is False
    assert runner.marketplace_ref == "v0.1.0"
    assert (claude.destination.parent / ".claude-uninstall.json").is_file()

    claude._journal_writer = original_writer
    retry_start = len(runner.calls)
    retry = manager.uninstall(consent=lambda: True)
    assert retry.status is SetupStatus.READY
    retry_mutations = [call[2:4] for call in runner.calls[retry_start:]]
    assert ["uninstall", "csaf@csaf"] not in retry_mutations
    assert ["marketplace", "remove"] in retry_mutations
    assert not (claude.destination.parent / ".claude-uninstall.json").exists()


def test_manager_state_checkpoint_failure_keeps_claude_uninstall_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _codex, claude, runner = _actual_crash_manager(tmp_path)
    first = manager.plan_install(_actual_crash_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    original = manager._write_uninstall_state

    def fail_claude_checkpoint(state: object, remaining: object) -> None:
        if isinstance(remaining, dict) and AssistantKind.CLAUDE not in remaining:
            raise SetupError("token=private-value C:\\Users\\Alice\\state.log")
        original(state, remaining)

    monkeypatch.setattr(manager, "_write_uninstall_state", fail_claude_checkpoint)
    result = manager.uninstall(consent=lambda: True)

    assert result.status is SetupStatus.PARTIAL
    assert (claude.destination.parent / ".claude-uninstall.json").is_file()
    state = json.loads((tmp_path / "data" / "state.json").read_text(encoding="utf-8"))
    assert "claude" in state["adapter_targets"]
    runner._load()
    assert runner.plugin is False
    assert runner.marketplace_ref is None

    monkeypatch.setattr(manager, "_write_uninstall_state", original)
    assert manager.uninstall(consent=lambda: True).status is SetupStatus.READY
    assert not (claude.destination.parent / ".claude-uninstall.json").exists()


def test_repair_reconciles_pending_claude_uninstall_before_reinstall(
    tmp_path: Path,
) -> None:
    manager, _codex, claude, runner = _actual_crash_manager(tmp_path)
    first = manager.plan_install(_actual_crash_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    checksum = first.adapter_assets[AssistantKind.CLAUDE].sha256

    assert _actual_uninstall_subprocess(tmp_path, "claude:receipt-deleted") == 73
    assert claude.health(claude.destination, first.manifest.version, checksum) is False
    with pytest.raises(SetupError, match="pending uninstall"):
        claude.install(tmp_path / "missing.asset", first.manifest.version)

    result = manager.repair(first, consent=lambda _: True)

    assert result.status is SetupStatus.READY
    assert manager.doctor() is True
    assert not (claude.destination.parent / ".claude-uninstall.json").exists()
    runner._load()
    assert runner.marketplace_ref == "v0.1.0"
    assert runner.plugin is True
    assert manager.uninstall(consent=lambda: True).status is SetupStatus.READY


def test_same_version_update_cannot_return_ready_with_pending_claude_uninstall(
    tmp_path: Path,
) -> None:
    manager, _codex, claude, runner = _actual_crash_manager(tmp_path)
    first = manager.plan_install(_actual_crash_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    assert _actual_uninstall_subprocess(tmp_path, "claude:receipt-deleted") == 73

    result = manager.update(first, consent=lambda _: True)

    assert result.status is SetupStatus.READY
    assert not (claude.destination.parent / ".claude-uninstall.json").exists()
    assert manager.doctor() is True
    runner._load()
    assert runner.marketplace_ref == "v0.1.0"
    assert runner.plugin is True


def test_doctor_reconciles_pending_claude_uninstall_before_health_result(
    tmp_path: Path,
) -> None:
    manager, _codex, claude, runner = _actual_crash_manager(tmp_path)
    first = manager.plan_install(_actual_crash_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY

    assert _actual_uninstall_subprocess(tmp_path, "claude:receipt-deleted") == 73
    assert (claude.destination.parent / ".claude-uninstall.json").is_file()

    assert manager.doctor() is True
    assert not (claude.destination.parent / ".claude-uninstall.json").exists()
    state = json.loads((tmp_path / "data" / "state.json").read_text(encoding="utf-8"))
    assert "claude" not in state["adapter_targets"]
    runner._load()
    assert runner.plugin is False
    assert runner.marketplace_ref is None


@pytest.mark.parametrize("phase", ["codex:entry", "internal-backup"])
def test_actual_codex_in_facade_crash_restores_manager_backup(tmp_path: Path, phase: str) -> None:
    manager, codex, _claude, _runner = _actual_crash_manager(tmp_path)
    first = manager.plan_install(_actual_crash_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    tree_before = _tree_bytes(codex.destination)
    if phase == "internal-backup":
        returncode = _codex_internal_backup_crash_subprocess(tmp_path, "0.2.0")
        assert returncode == 71
        assert not codex.destination.exists()
        assert (codex.destination.parent / ".csaf.backup").is_dir()
        transaction = next((tmp_path / "data" / ".staging").iterdir())
        journal = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
        recovery = journal["adapters"]["codex"]["recovery"]
        assert recovery == {
            "target_existed": True,
            "task3_staging_existed": False,
            "task3_backup_existed": False,
        }
    else:
        returncode = _actual_crash_subprocess(tmp_path, phase, "0.2.0")
    assert returncode == 71

    assert manager.doctor() is True
    assert _tree_bytes(codex.destination) == tree_before
    assert not (codex.destination.parent / ".csaf.backup").exists()
    assert not (codex.destination.parent / ".csaf.staging").exists()


@pytest.mark.parametrize("phase", ["claude:marketplace-added", "claude:plugin-installed"])
def test_fresh_claude_in_facade_crash_removes_only_created_external_state(
    tmp_path: Path, phase: str
) -> None:
    manager, _codex, claude, runner = _actual_crash_manager(tmp_path)

    assert _actual_crash_subprocess(tmp_path, phase, "0.1.0") == 71
    assert manager.doctor() is False

    runner._load()
    assert runner.marketplace_ref is None
    assert runner.plugin is False
    assert not claude.destination.exists()
    assert not any((tmp_path / "data" / ".staging").iterdir())


def test_claude_recovery_is_idempotent_after_second_process_crash(tmp_path: Path) -> None:
    manager, _codex, claude, runner = _actual_crash_manager(tmp_path)
    assert _actual_crash_subprocess(tmp_path, "claude:plugin-installed", "0.1.0") == 71

    assert _actual_recovery_subprocess(tmp_path, "claude:plugin-removed") == 72
    assert any((tmp_path / "data" / ".staging").iterdir())

    assert manager.doctor() is False
    runner._load()
    assert runner.marketplace_ref is None
    assert runner.plugin is False
    assert not claude.destination.exists()
    assert not any((tmp_path / "data" / ".staging").iterdir())


def test_claude_recovery_retains_journal_on_unexpected_external_version(tmp_path: Path) -> None:
    manager, _codex, _claude, runner = _actual_crash_manager(tmp_path)
    assert _actual_crash_subprocess(tmp_path, "claude:marketplace-added", "0.1.0") == 71
    runner.marketplace_ref = "v9.9.9"
    runner._save()
    start = len(runner.calls)

    assert manager.doctor() is False

    runner._load()
    assert runner.marketplace_ref == "v9.9.9"
    assert runner.plugin is False
    destructive = [call[2:4] for call in runner.calls[start:]]
    assert ["marketplace", "remove"] not in destructive
    assert ["uninstall", "csaf@csaf"] not in destructive
    assert any((tmp_path / "data" / ".staging").iterdir())


@pytest.mark.parametrize(
    ("plugin_before", "phase"),
    [(False, "claude:plugin-installed"), (True, "claude:entry")],
)
def test_fresh_claude_crash_preserves_preexisting_external_state(
    tmp_path: Path, plugin_before: bool, phase: str
) -> None:
    manager, _codex, claude, runner = _actual_crash_manager(tmp_path)
    runner.marketplace_ref = "v0.1.0"
    runner.plugin = plugin_before
    runner._save()

    assert _actual_crash_subprocess(tmp_path, phase, "0.1.0") == 71
    transaction = next((tmp_path / "data" / ".staging").iterdir())
    journal = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
    recovery = journal["adapters"]["claude"]["recovery"]
    assert recovery["installing_version"] == "0.1.0"
    assert recovery["marketplace"] == {
        "name": "csaf",
        "source": "https://github.com/karthiknambiar/csm-skills-framework.git",
        "ref": "v0.1.0",
    }
    assert recovery["plugin"] == ({"name": "csaf@csaf", "scope": "user"} if plugin_before else None)
    assert manager.doctor() is False

    runner._load()
    assert runner.marketplace_ref == "v0.1.0"
    assert runner.plugin is plugin_before
    assert not claude.destination.exists()


@pytest.mark.parametrize(
    "phase",
    [
        "claude:old-plugin-removed",
        "claude:old-marketplace-removed",
        "claude:marketplace-added",
        "claude:plugin-installed",
    ],
)
def test_owned_claude_transition_crash_restores_exact_old_external_state(
    tmp_path: Path, phase: str
) -> None:
    manager, _codex, claude, runner = _actual_crash_manager(tmp_path)
    first = manager.plan_install(_actual_crash_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    receipt_before = (claude.destination / "receipt.json").read_bytes()
    current_before = (tmp_path / "data" / "current.json").read_bytes()

    assert _actual_crash_subprocess(tmp_path, phase, "0.2.0") == 71
    assert manager.doctor() is True

    runner._load()
    assert runner.marketplace_ref == "v0.1.0"
    assert runner.plugin is True
    assert (claude.destination / "receipt.json").read_bytes() == receipt_before
    assert (tmp_path / "data" / "current.json").read_bytes() == current_before


def test_actual_adapters_restore_external_state_after_process_crash(tmp_path: Path) -> None:
    manager, codex, claude, runner = _actual_crash_manager(tmp_path)
    first = manager.plan_install(_actual_crash_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    codex_before = _tree_bytes(codex.destination)
    claude_before = _tree_bytes(claude.destination)
    state_before = (tmp_path / "data" / "state.json").read_bytes()
    current_before = (tmp_path / "data" / "current.json").read_bytes()

    assert _actual_crash_subprocess(tmp_path, "adapter:claude:mutated", "0.2.0") == 71

    assert manager.doctor() is True
    runner._load()
    assert runner.marketplace_ref == "v0.1.0"
    assert runner.plugin is True
    assert _tree_bytes(codex.destination) == codex_before
    assert _tree_bytes(claude.destination) == claude_before
    assert (tmp_path / "data" / "state.json").read_bytes() == state_before
    assert (tmp_path / "data" / "current.json").read_bytes() == current_before


@pytest.mark.parametrize("phase", ["adapter:codex:mutated", "adapter:claude:mutated"])
def test_subprocess_adapter_crash_restores_previous_installation(
    tmp_path: Path,
    phase: str,
) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)
    first = manager.plan_install(_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY
    state_before = (tmp_path / "data" / "state.json").read_bytes()
    current_before = (tmp_path / "data" / "current.json").read_bytes()
    adapter_before = {kind: _tree_bytes(adapter.destination) for kind, adapter in adapters.items()}

    assert _crash_install_subprocess(tmp_path, phase, "0.2.0") == 71

    assert manager.doctor() is True
    assert (tmp_path / "data" / "state.json").read_bytes() == state_before
    assert (tmp_path / "data" / "current.json").read_bytes() == current_before
    assert {
        kind: _tree_bytes(adapter.destination) for kind, adapter in adapters.items()
    } == adapter_before
    assert not any((tmp_path / "data" / ".staging").iterdir())


def test_subprocess_fresh_adapter_crash_removes_only_created_target(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, adapters = _manager(tmp_path, harness)

    assert _crash_install_subprocess(tmp_path, "adapter:codex:mutated", "0.1.0") == 71

    result = manager.install(
        manager.plan_install(_manifest("0.1.0"), requested_targets=None),
        consent=lambda _: True,
    )
    assert result.status is SetupStatus.READY
    assert all(adapter.health_ready for adapter in adapters.values())


def test_crash_after_current_before_commit_marker_rolls_back(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness)
    first = manager.plan_install(_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY

    assert _crash_install_subprocess(tmp_path, "current-replaced", "0.2.0") == 71

    assert manager.doctor() is True
    assert manager._active_version() == first.manifest.version


def test_crash_after_durable_commit_retains_new_installation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    manager, _ = _manager(tmp_path, harness)
    first = manager.plan_install(_manifest("0.1.0"), requested_targets=None)
    assert manager.install(first, consent=lambda _: True).status is SetupStatus.READY

    assert _crash_install_subprocess(tmp_path, "committed", "0.2.0") == 71

    assert manager.doctor() is True
    assert manager._active_version() == _manifest("0.2.0").version
    assert not any((tmp_path / "data" / ".staging").iterdir())
