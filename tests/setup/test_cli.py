"""Native setup CLI contracts."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from csaf.setup.assets import SetupError
from csaf.setup.manager import SetupResult, SetupStatus
from csaf.setup.paths import current_platform
from csaf.setup.types import (
    AssistantKind,
    InstallState,
    OfficeCLIDependency,
    ReleaseAsset,
    ReleaseManifest,
    SupportedPlatform,
    Version,
)

runner = CliRunner()


def _asset(name: str) -> ReleaseAsset:
    return ReleaseAsset(
        url=f"https://example.test/{name}",
        sha256="a" * 64,
        size=123,
    )


def _manifest(version: str = "0.2.0") -> ReleaseManifest:
    platforms = {platform: _asset(f"runtime-{platform.value}") for platform in SupportedPlatform}
    office = {platform: _asset(f"officecli-{platform.value}") for platform in SupportedPlatform}
    return ReleaseManifest(
        schema_version=1,
        version=Version(version),
        runtime=platforms,
        codex_skill=_asset("codex.zip"),
        claude_plugin=_asset("claude.zip"),
        officecli=OfficeCLIDependency(
            version=Version("1.0.143"),
            minimum_version=Version("1.0.137"),
            assets=office,
        ),
    )


def _write_installed_state(
    root: Path,
    *,
    version: str = "0.1.0",
    assistants: tuple[AssistantKind, ...] = (AssistantKind.CODEX, AssistantKind.CLAUDE),
    office_owned: bool = True,
) -> InstallState:
    active = Version(version)
    runtime = root / "versions" / version
    office = root / "officecli" / "1.0.143" / "officecli"
    state = InstallState(
        active_version=active,
        installed_versions=(active,),
        runtime_paths={active: runtime},
        verified_checksums={},
        adapter_targets={kind: root / "recorded-adapters" / kind.value for kind in assistants},
        officecli_version=Version("1.0.143"),
        officecli_path=office,
        officecli_sha256="a" * 64,
        officecli_installed_by_csaf=office_owned,
        installed_at=datetime(2026, 8, 11, tzinfo=UTC),
        updated_at=datetime(2026, 8, 11, tzinfo=UTC),
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(state.model_dump_json(), encoding="utf-8")
    (root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_version": version,
                "runtime_path": str(runtime),
            }
        ),
        encoding="utf-8",
    )
    return state


class FakeResolver:
    def __init__(self, manifest: ReleaseManifest | None = None) -> None:
        self.manifest = manifest or _manifest()
        self.sources: list[str | None] = []
        self.versions: list[Version] = []

    def resolve(self, source: str | None = None) -> ReleaseManifest:
        self.sources.append(source)
        return self.manifest

    def resolve_version(self, version: Version) -> ReleaseManifest:
        self.versions.append(version)
        return _manifest(str(version))


class FakeManager:
    def __init__(
        self,
        root: Path,
        *,
        targets: tuple[AssistantKind, ...] = (
            AssistantKind.CODEX,
            AssistantKind.CLAUDE,
            AssistantKind.GEMINI,
        ),
    ) -> None:
        self.root = root
        self.targets = targets
        self.calls: list[tuple[str, Any]] = []
        self.result = SetupResult(SetupStatus.READY, Version("0.2.0"), True)
        self.active_version = Version("0.1.0")
        self.ready = True

    def plan_install(
        self,
        manifest: ReleaseManifest,
        *,
        requested_targets: tuple[AssistantKind, ...] | None,
    ) -> SimpleNamespace:
        self.calls.append(("plan_install", requested_targets))
        selected = self.targets if requested_targets is None else requested_targets
        return SimpleNamespace(
            manifest=manifest,
            targets=selected,
            data_root=self.root,
            runtime_path=self.root / "versions" / str(manifest.version),
            officecli_path=self.root / "officecli" / "1.0.143" / "officecli",
            adapter_destinations={kind: self.root / "adapters" / kind.value for kind in selected},
            already_healthy=False,
        )

    def install(self, plan: object, *, consent: object, assume_yes: bool) -> SetupResult:
        self.calls.append(("install", assume_yes))
        return self.result

    def repair(self, plan: object, *, consent: object, assume_yes: bool) -> SetupResult:
        self.calls.append(("repair", assume_yes))
        return self.result

    def update(self, plan: object, *, consent: object, assume_yes: bool) -> SetupResult:
        self.calls.append(("update", assume_yes))
        return self.result

    def check_update(self, manifest: ReleaseManifest) -> bool:
        self.calls.append(("check_update", str(manifest.version)))
        return self.active_version < manifest.version

    def doctor(self) -> bool:
        self.calls.append(("doctor", None))
        return self.ready

    def uninstall(
        self,
        *,
        consent: object,
        include_officecli: bool,
        assume_yes: bool,
    ) -> SetupResult:
        self.calls.append(("uninstall", (include_officecli, assume_yes)))
        return self.result


@pytest.fixture
def setup_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Any, FakeManager, FakeResolver]:
    from csaf.setup import cli

    manager = FakeManager(tmp_path / "CSAF")
    _write_installed_state(manager.root)
    resolver = FakeResolver()
    monkeypatch.setattr(cli, "_make_manager", lambda: manager)
    monkeypatch.setattr(cli, "_make_resolver", lambda: resolver)
    return cli, manager, resolver


def test_setup_help_registers_every_lifecycle_command(setup_cli: tuple[Any, Any, Any]) -> None:
    cli, _, _ = setup_cli

    result = runner.invoke(cli.setup_app, ["--help"])

    assert result.exit_code == 0
    for command in ("install", "doctor", "repair", "check-update", "update", "uninstall"):
        assert command in result.stdout


def test_install_discloses_complete_plan_before_prompt_and_decline_is_exit_two(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, _ = setup_cli

    result = runner.invoke(cli.setup_app, ["install"], input="n\n")

    assert result.exit_code == 2
    prompt = result.stdout.index("Proceed with installation?")
    for disclosure in (
        "CSAF version: 0.2.0",
        "OfficeCLI version: 1.0.143",
        "mandatory for QBR PowerPoint and Word generation",
        "Assistants: codex, claude, gemini",
        str(manager.root / "versions" / "0.2.0"),
        str(manager.root / "officecli" / "1.0.143" / "officecli"),
        "Network: HTTPS downloads of verified CSAF, OfficeCLI, and adapter assets",
    ):
        assert disclosure in result.stdout
        assert result.stdout.index(disclosure) < prompt
    assert [call[0] for call in manager.calls] == ["plan_install"]


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ("--codex-only", (AssistantKind.CODEX,)),
        ("--claude-only", (AssistantKind.CLAUDE,)),
        ("--gemini-only", (AssistantKind.GEMINI,)),
    ],
)
def test_install_yes_selects_target_and_skips_prompt(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
    option: str,
    expected: tuple[AssistantKind, ...],
) -> None:
    cli, manager, _ = setup_cli

    result = runner.invoke(cli.setup_app, ["install", "--yes", option])

    assert result.exit_code == 0
    assert "Proceed" not in result.stdout
    assert manager.calls == [("plan_install", expected), ("install", True)]


def test_install_rejects_two_target_overrides_before_resolution(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, resolver = setup_cli

    result = runner.invoke(
        cli.setup_app,
        ["install", "--yes", "--codex-only", "--claude-only"],
    )

    assert result.exit_code == 2
    assert "choose only one" in result.stderr.lower()
    assert manager.calls == []
    assert resolver.sources == []


def test_install_forwards_explicit_manifest_source(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, _, resolver = setup_cli
    source = "https://example.test/releases/v0.2.0/manifest.json"

    result = runner.invoke(cli.setup_app, ["install", "--yes", "--manifest", source])

    assert result.exit_code == 0
    assert resolver.sources == [source]
    assert f"Release source: {source}" in result.stdout


def test_install_without_assistant_reports_runtime_only_guidance(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, _ = setup_cli
    manager.targets = ()

    result = runner.invoke(cli.setup_app, ["install", "--yes"])

    assert result.exit_code == 0
    assert "Assistants: none detected" in result.stdout
    assert "No native adapter will be added" in result.stdout
    assert "csaf setup repair" in result.stdout


@pytest.mark.parametrize("status", [SetupStatus.CANCELLED, SetupStatus.FAILED, SetupStatus.PARTIAL])
def test_nonready_lifecycle_result_exits_two_and_sanitizes_error(
    setup_cli: tuple[Any, FakeManager, FakeResolver], status: SetupStatus
) -> None:
    cli, manager, _ = setup_cli
    secret = "sk" + "-" + "A" * 32
    manager.result = SetupResult(status, Version("0.1.0"), True, f"token={secret} C:\\Users\\A\\x")

    result = runner.invoke(cli.setup_app, ["install", "--yes"])

    assert result.exit_code == 2
    assert secret not in result.output
    assert r"C:\Users\A\x" not in result.output
    assert "Traceback" not in result.output
    assert "Next action: csaf setup doctor" in result.stdout


def test_doctor_human_and_json_are_deterministic(setup_cli: tuple[Any, FakeManager, Any]) -> None:
    cli, manager, _ = setup_cli

    human = runner.invoke(cli.setup_app, ["doctor"])
    structured = runner.invoke(cli.setup_app, ["doctor", "--json"])

    assert human.exit_code == 0
    assert human.stdout == "Status: ready\nNext action: none\n"
    assert structured.exit_code == 0
    assert structured.stdout == (
        json.dumps({"next_action": "none", "status": "ready"}, indent=2, sort_keys=True) + "\n"
    )
    assert [call[0] for call in manager.calls] == ["doctor", "doctor"]


def test_doctor_unhealthy_exits_two_with_repair_guidance(
    setup_cli: tuple[Any, FakeManager, Any],
) -> None:
    cli, manager, _ = setup_cli
    manager.ready = False

    result = runner.invoke(cli.setup_app, ["doctor"])

    assert result.exit_code == 2
    assert "Status: failed" in result.stdout
    assert "csaf setup repair" in result.stdout


@pytest.mark.parametrize("command", ["repair", "update"])
def test_mutating_lifecycle_commands_require_consent(
    setup_cli: tuple[Any, FakeManager, Any], command: str
) -> None:
    cli, manager, _ = setup_cli

    result = runner.invoke(cli.setup_app, [command], input="n\n")

    assert result.exit_code == 2
    expected = ["plan_install"] if command == "repair" else ["check_update", "plan_install"]
    assert [name for name, _ in manager.calls] == expected


@pytest.mark.parametrize("command", ["repair", "update"])
def test_mutating_lifecycle_commands_call_only_requested_operation(
    setup_cli: tuple[Any, FakeManager, Any], command: str
) -> None:
    cli, manager, _ = setup_cli

    result = runner.invoke(cli.setup_app, [command, "--yes"])

    assert result.exit_code == 0
    expected = (
        ["plan_install", "repair"]
        if command == "repair"
        else ["check_update", "plan_install", "update"]
    )
    assert [name for name, _ in manager.calls] == expected


def test_uninstall_discloses_officecli_policy_and_forwards_flag(
    setup_cli: tuple[Any, FakeManager, Any],
) -> None:
    cli, manager, _ = setup_cli

    result = runner.invoke(cli.setup_app, ["uninstall", "--yes", "--include-officecli"])

    assert result.exit_code == 0
    assert "OfficeCLI will be removed only if CSAF installed it" in result.stdout
    assert manager.calls == [("uninstall", (True, True))]


def test_uninstall_decline_does_not_call_manager(
    setup_cli: tuple[Any, FakeManager, Any],
) -> None:
    cli, manager, _ = setup_cli

    result = runner.invoke(cli.setup_app, ["uninstall"], input="n\n")

    assert result.exit_code == 2
    prompt = result.stdout.index("Proceed with uninstall?")
    for disclosure in (
        "CSAF version: 0.1.0",
        "OfficeCLI version: 1.0.143",
        "Assistants: codex, claude",
        str(manager.root),
        "Network: none",
    ):
        assert disclosure in result.stdout
        assert result.stdout.index(disclosure) < prompt
    assert manager.calls == []


def test_update_cache_uses_network_at_most_once_in_24_hours(tmp_path: Path) -> None:
    from csaf.setup.cli import UpdateCache

    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    calls = 0

    def fetch(_source: str | None = None) -> ReleaseManifest:
        nonlocal calls
        calls += 1
        return _manifest()

    cache = UpdateCache(tmp_path, resolver=fetch, clock=lambda: now)
    first = cache.check(Version("0.1.0"))
    now += timedelta(hours=23, minutes=59)
    second = cache.check(Version("0.1.0"))
    now += timedelta(minutes=1)
    third = cache.check(Version("0.1.0"))

    assert calls == 2
    assert first.available is second.available is third.available is True
    assert first.available_version == second.available_version == third.available_version


def test_update_cache_network_failure_is_nonfatal(tmp_path: Path) -> None:
    from csaf.setup.cli import UpdateCache

    def offline(_source: str | None = None) -> ReleaseManifest:
        raise OSError("token=secret-value C:\\Users\\Alice\\network.log")

    report = UpdateCache(tmp_path, resolver=offline).check(Version("0.1.0"))

    assert report.available is False
    assert report.installed_version == Version("0.1.0")
    assert report.offline is True
    assert report.error == "update check is unavailable"


def test_check_update_notifies_without_invoking_update(
    setup_cli: tuple[Any, FakeManager, FakeResolver], monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, manager, _ = setup_cli
    report = cli.UpdateReport(
        installed_version=Version("0.1.0"),
        available_version=Version("0.2.0"),
        available=True,
        cached=True,
        offline=False,
    )
    monkeypatch.setattr(cli, "_check_update", lambda _manager, _resolver: report)

    result = runner.invoke(cli.setup_app, ["check-update"])

    assert result.exit_code == 0
    assert "Installed version: 0.1.0" in result.stdout
    assert "Available version: 0.2.0" in result.stdout
    assert "csaf setup update" in result.stdout
    assert manager.calls == []


def test_update_with_no_available_release_is_a_successful_read_only_noop(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, resolver = setup_cli
    manager.active_version = resolver.manifest.version

    result = runner.invoke(cli.setup_app, ["update"])

    assert result.exit_code == 0
    assert "No stable CSAF update is available" in result.stdout
    assert "Proceed" not in result.stdout
    assert [name for name, _ in manager.calls] == ["check_update"]


def test_check_update_json_is_sorted_and_notification_only(
    setup_cli: tuple[Any, FakeManager, FakeResolver], monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, manager, _ = setup_cli
    report = cli.UpdateReport(
        installed_version=Version("0.1.0"),
        available_version=Version("0.2.0"),
        available=True,
        cached=True,
        offline=False,
    )
    monkeypatch.setattr(cli, "_check_update", lambda _manager, _resolver: report)

    result = runner.invoke(cli.setup_app, ["check-update", "--json"])

    assert result.exit_code == 0
    expected = json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n"
    assert result.stdout == expected
    assert manager.calls == []


def test_release_resolver_rejects_credential_bearing_url_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from csaf.setup import cli

    monkeypatch.setattr(
        cli.urllib.request,
        "build_opener",
        lambda *_handlers: pytest.fail("credential URL reached the network boundary"),
    )

    with pytest.raises(SetupError, match="credentials"):
        cli.ReleaseResolver().resolve("https://user:password@example.test/manifest.json")


def test_manifest_source_disclosure_removes_terminal_controls(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, _, _ = setup_cli
    source = "https://example.test/\x1b[31mmanifest.json"

    result = runner.invoke(cli.setup_app, ["install", "--yes", "--manifest", source])

    assert result.exit_code == 0
    assert "\x1b" not in result.output


def test_repair_resolves_exact_active_release_instead_of_latest(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, resolver = setup_cli
    manager.result = SetupResult(SetupStatus.READY, Version("0.1.0"), True)

    result = runner.invoke(cli.setup_app, ["repair", "--yes"])

    assert result.exit_code == 0
    assert resolver.sources == []
    assert resolver.versions == [Version("0.1.0")]
    assert manager.calls[0] == (
        "plan_install",
        (AssistantKind.CODEX, AssistantKind.CLAUDE),
    )
    assert "CSAF version: 0.1.0" in result.stdout
    assert "CSAF version: 0.2.0" not in result.stdout


def test_repair_without_valid_active_installation_exits_before_network(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, resolver = setup_cli
    (manager.root / "current.json").unlink()

    result = runner.invoke(cli.setup_app, ["repair", "--yes"])

    assert result.exit_code == 2
    assert "csaf setup install" in result.output
    assert resolver.sources == []
    assert resolver.versions == []
    assert manager.calls == []


def test_uninstall_disclosure_uses_recorded_state_not_current_detection(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, _ = setup_cli
    manager.targets = (AssistantKind.CODEX,)
    state = _write_installed_state(
        manager.root,
        assistants=(AssistantKind.CLAUDE,),
        office_owned=False,
    )
    before = {
        path.relative_to(manager.root): path.read_bytes()
        for path in manager.root.rglob("*")
        if path.is_file()
    }

    result = runner.invoke(cli.setup_app, ["uninstall"], input="n\n")

    after = {
        path.relative_to(manager.root): path.read_bytes()
        for path in manager.root.rglob("*")
        if path.is_file()
    }
    assert result.exit_code == 2
    assert "Assistants: claude" in result.stdout
    assert "Assistants: codex" not in result.stdout
    assert "OfficeCLI ownership: not installed by CSAF" in result.stdout
    assert str(state.runtime_paths[state.active_version]) in result.stdout
    assert str(state.officecli_path) in result.stdout
    assert str(state.adapter_targets[AssistantKind.CLAUDE]) in result.stdout
    assert before == after
    assert manager.calls == []


@pytest.mark.parametrize(
    "location",
    [
        r"C:\Users\Alice\manifest.json",
        r"\\server\share\manifest.json",
    ],
)
def test_release_resolver_treats_absolute_windows_paths_as_local_before_urlsplit(
    location: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from csaf.setup import cli

    monkeypatch.setattr(
        cli.urllib.request,
        "build_opener",
        lambda *_handlers: pytest.fail("absolute local path reached the network boundary"),
    )

    with pytest.raises(SetupError, match="could not be resolved"):
        cli.ReleaseResolver().resolve(location)


@pytest.mark.parametrize("location", ["", "manifest.json", "../manifest.json", r"C:manifest.json"])
def test_release_resolver_rejects_ambiguous_or_relative_local_paths(
    location: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from csaf.setup import cli

    monkeypatch.setattr(
        cli.urllib.request,
        "build_opener",
        lambda *_handlers: pytest.fail("ambiguous local path reached the network boundary"),
    )
    with pytest.raises(SetupError, match="absolute"):
        cli.ReleaseResolver().resolve(location)


def test_release_resolver_reads_absolute_local_manifest(tmp_path: Path) -> None:
    from csaf.setup import cli

    manifest = tmp_path / "manifest.json"
    manifest.write_text(_manifest("0.1.0").model_dump_json(), encoding="utf-8")

    resolved = cli.ReleaseResolver().resolve(str(manifest.resolve()))

    assert resolved.version == Version("0.1.0")


def test_resolve_version_uses_explicit_absolute_local_manifest_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from csaf.setup import cli

    manifest_path = tmp_path / "installed-manifest.json"
    manifest_path.write_text(_manifest("0.1.0").model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(
        cli.urllib.request,
        "build_opener",
        lambda *_handlers: pytest.fail("explicit local manifest reached the network boundary"),
    )

    manifest = cli.ReleaseResolver().resolve_version(
        Version("0.1.0"), source=str(manifest_path.resolve())
    )

    assert manifest.version == Version("0.1.0")


def test_repair_rejects_boolean_current_schema_before_network(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, resolver = setup_cli
    current_path = manager.root / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["schema_version"] = True
    current_path.write_text(json.dumps(current), encoding="utf-8")

    result = runner.invoke(cli.setup_app, ["repair", "--yes"])

    assert result.exit_code == 2
    assert "active runtime" in result.output
    assert resolver.sources == []
    assert resolver.versions == []
    assert manager.calls == []


@pytest.mark.parametrize(
    "location",
    [
        r"C:\safe\..\manifest.json",
        r"\\server\share\..\manifest.json",
        "/safe/../manifest.json",
    ],
)
def test_release_resolver_rejects_absolute_local_traversal_before_io(
    location: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from csaf.setup import cli

    monkeypatch.setattr(
        cli.urllib.request,
        "build_opener",
        lambda *_handlers: pytest.fail("traversal path reached the network boundary"),
    )

    with pytest.raises(SetupError, match="traversal"):
        cli.ReleaseResolver().resolve(location)


@pytest.mark.parametrize("current_condition", ["missing", "corrupt"])
def test_uninstall_uses_valid_recorded_state_when_current_is_unusable(
    setup_cli: tuple[Any, FakeManager, FakeResolver], current_condition: str
) -> None:
    cli, manager, _ = setup_cli
    current = manager.root / "current.json"
    if current_condition == "missing":
        current.unlink()
    else:
        current.write_text("{not-json", encoding="utf-8")
    before = {
        path.relative_to(manager.root): path.read_bytes()
        for path in manager.root.rglob("*")
        if path.is_file()
    }

    declined = runner.invoke(cli.setup_app, ["uninstall"], input="n\n")

    after = {
        path.relative_to(manager.root): path.read_bytes()
        for path in manager.root.rglob("*")
        if path.is_file()
    }
    assert declined.exit_code == 2
    assert "CSAF version: 0.1.0" in declined.stdout
    assert "OfficeCLI version: 1.0.143" in declined.stdout
    assert "OfficeCLI ownership: installed by CSAF" in declined.stdout
    assert "Assistants: codex, claude" in declined.stdout
    assert str(manager.root / "versions" / "0.1.0") in declined.stdout
    assert str(manager.root / "officecli" / "1.0.143" / "officecli") in declined.stdout
    assert str(manager.root / "recorded-adapters" / "codex") in declined.stdout
    assert str(manager.root / "recorded-adapters" / "claude") in declined.stdout
    assert before == after
    assert manager.calls == []

    approved = runner.invoke(cli.setup_app, ["uninstall", "--yes"])

    assert approved.exit_code == 0
    assert manager.calls == [("uninstall", (False, True))]


def test_uninstall_discloses_partial_state_without_active_pointer(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, _ = setup_cli
    state = _write_installed_state(manager.root)
    partial = state.model_copy(update={"active_version": None})
    (manager.root / "state.json").write_text(partial.model_dump_json(), encoding="utf-8")
    (manager.root / "current.json").unlink()

    result = runner.invoke(cli.setup_app, ["uninstall"], input="n\n")

    assert result.exit_code == 2
    assert "CSAF version: 0.1.0 (not active)" in result.stdout
    assert str(partial.runtime_paths[Version("0.1.0")]) in result.stdout
    assert manager.calls == []


def test_uninstall_rejects_invalid_recorded_state_before_prompt_or_manager(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, _ = setup_cli
    (manager.root / "state.json").write_text("{not-json", encoding="utf-8")

    result = runner.invoke(cli.setup_app, ["uninstall", "--yes"])

    assert result.exit_code == 2
    assert "installed CSAF state is invalid" in result.output
    assert "Proceed with uninstall?" not in result.output
    assert manager.calls == []


def test_manifest_source_disclosure_drops_query_fragment_and_secret(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, _, _ = setup_cli
    token = "tok" + "en-value-123456789"
    source = f"https://example.test/releases/manifest.json?access_token={token}#private"

    result = runner.invoke(cli.setup_app, ["install", "--yes", "--manifest", source])

    assert result.exit_code == 0
    assert "Release source: https://example.test/releases/manifest.json" in result.stdout
    assert token not in result.output
    assert "access_token" not in result.output
    assert "#private" not in result.output


def test_release_resolver_rejects_query_or_fragment_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from csaf.setup import cli

    monkeypatch.setattr(
        cli.urllib.request,
        "build_opener",
        lambda *_handlers: pytest.fail("secret-bearing URL reached the network boundary"),
    )
    secret = "sec" + "ret-query-value"

    with pytest.raises(SetupError, match="query or fragment"):
        cli.ReleaseResolver().resolve(
            f"https://example.test/manifest.json?access_token={secret}#fragment"
        )


@pytest.mark.parametrize(
    "failure",
    [
        http.client.RemoteDisconnected("remote closed"),
        http.client.IncompleteRead(b"partial", 100),
        ssl.SSLError("TLS failed"),
    ],
)
def test_release_resolver_translates_bounded_network_failures(
    failure: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    from csaf.setup import cli

    class FailingOpener:
        def open(self, request: object, *, timeout: float) -> object:
            del request, timeout
            raise failure

    monkeypatch.setattr(cli.urllib.request, "build_opener", lambda *_handlers: FailingOpener())

    with pytest.raises(SetupError, match="could not be resolved") as raised:
        cli.ReleaseResolver().resolve("https://example.test/manifest.json")

    assert "remote closed" not in str(raised.value)
    assert "partial" not in str(raised.value)
    assert "TLS failed" not in str(raised.value)


@pytest.mark.parametrize(
    "location",
    [r"\\.\pipe\manifest.json", r"\\?\C:\manifest.json", r"C:\CON", r"C:\AUX.json"],
)
def test_release_resolver_rejects_windows_devices_before_open(location: str) -> None:
    from csaf.setup import cli

    with pytest.raises(SetupError, match="device"):
        cli.ReleaseResolver().resolve(location)


def test_release_resolver_rejects_symlink_manifest(tmp_path: Path) -> None:
    from csaf.setup import cli

    source = tmp_path / "source.json"
    source.write_text(_manifest().model_dump_json(), encoding="utf-8")
    link = tmp_path / "manifest-link.json"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(SetupError, match="regular file"):
        cli.ReleaseResolver().resolve(str(link.resolve(strict=False) if False else link.absolute()))


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "mkfifo"), reason="POSIX FIFO contract")
def test_release_resolver_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    from csaf.setup import cli

    fifo = tmp_path / "manifest.fifo"
    os.mkfifo(fifo)
    started = time.monotonic()

    with pytest.raises(SetupError, match="regular file"):
        cli.ReleaseResolver().resolve(str(fifo.absolute()))

    assert time.monotonic() - started < 1.0


def test_update_cache_serializes_concurrent_process_boundary(tmp_path: Path) -> None:
    from csaf.setup.cli import UpdateCache

    calls = 0
    entered = threading.Event()
    release = threading.Event()

    def fetch(_source: str | None = None) -> ReleaseManifest:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return _manifest()

    caches = [UpdateCache(tmp_path, resolver=fetch), UpdateCache(tmp_path, resolver=fetch)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cache.check, Version("0.1.0")) for cache in caches]
        assert entered.wait(timeout=2)
        time.sleep(0.1)
        release.set()
        reports = [future.result(timeout=3) for future in futures]

    assert calls == 1
    assert all(report.available for report in reports)


def test_update_cache_releases_lock_after_fetch_failure(tmp_path: Path) -> None:
    from csaf.setup.cli import UpdateCache

    failed = UpdateCache(tmp_path, resolver=lambda _source=None: (_ for _ in ()).throw(OSError()))
    assert failed.check(Version("0.1.0")).offline is True

    recovered = UpdateCache(tmp_path, resolver=lambda _source=None: _manifest())
    assert recovered.check(Version("0.1.0")).available is True


def _runtime_bundle(
    tmp_path: Path,
    *,
    platform: str | None = None,
    version: str = "0.1.0",
    extra_member: tuple[str, bytes] | None = None,
) -> Path:
    runtime = b"dummy csaf wheel"
    runtime_name = f"csaf-{version}-py3-none-any.whl"
    dependency = b"dummy dependency wheel"
    runtime_hash = hashlib.sha256(runtime).hexdigest()
    dependency_hash = hashlib.sha256(dependency).hexdigest()
    requirements = (
        f"./{runtime_name} --hash=sha256:{runtime_hash}\n"
        f"dummy-dependency==1.0.0 --hash=sha256:{dependency_hash}\n"
    ).encode()
    members = {
        runtime_name: runtime,
        "requirements.lock": requirements,
        "wheelhouse/dummy_dependency-1.0.0-py3-none-any.whl": dependency,
    }
    manifest = {
        "schema_version": 1,
        "version": version,
        "platform": platform or current_platform().value,
        "files": {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for name, content in members.items()
        },
    }
    bundle = tmp_path / "runtime-bundle.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("runtime-bundle.json", json.dumps(manifest))
        for name, content in members.items():
            archive.writestr(name, content)
        if extra_member is not None:
            archive.writestr(*extra_member)
    return bundle


def test_runtime_installer_uses_private_uv_offline_exact_argv(tmp_path: Path) -> None:
    from csaf.setup import cli

    wheel = _runtime_bundle(tmp_path)
    uv = tmp_path / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    uv.parent.mkdir()
    uv.write_bytes(b"uv")
    launcher = tmp_path / ("bootstrap-csaf.exe" if os.name == "nt" else "bootstrap-csaf")
    launcher.write_bytes(b"launcher")
    destination = tmp_path / "transaction" / "runtime"
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(arguments: list[str], **options: Any) -> SimpleNamespace:
        calls.append((arguments, options))
        (destination / "csaf").mkdir(parents=True)
        (destination / "csaf" / "__init__.py").write_text("", encoding="utf-8")
        metadata = destination / "csaf-0.2.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: csaf\n", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    result = cli._install_runtime(
        wheel,
        destination,
        uv_path=uv,
        launcher_path=launcher,
        python_executable=Path(sys.executable),
        expected_version=Version("0.1.0"),
        runner=run,
    )

    assert result == destination
    assert calls[0][0] == [
        str(uv),
        "pip",
        "install",
        "--python",
        str(Path(sys.executable)),
        "--target",
        str(destination),
        "--offline",
        "--no-config",
        "--no-index",
        "--require-hashes",
        "--find-links",
        str(destination.parent / ".runtime-bundle" / "wheelhouse"),
        "--requirement",
        str(destination.parent / ".runtime-bundle" / "requirements.lock"),
    ]
    assert calls[0][1]["cwd"] == destination.parent / ".runtime-bundle"
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    assert calls[0][1]["env"]["UV_UNMANAGED_INSTALL"] == str(tmp_path / "bin")
    assert calls[0][1]["env"]["UV_PYTHON_INSTALL_DIR"] == str(tmp_path / "python")
    assert calls[0][1]["env"]["UV_CACHE_DIR"] == str(tmp_path / "cache" / "uv")
    assert (destination / ("csaf.exe" if os.name == "nt" else "csaf")).read_bytes() == b"launcher"


def test_runtime_installer_rejects_inner_version_mismatch_before_process(tmp_path: Path) -> None:
    from csaf.setup import cli

    bundle = _runtime_bundle(tmp_path, version="9.9.9")
    uv = tmp_path / "uv.exe"
    uv.write_bytes(b"uv")
    launcher = tmp_path / "csaf.exe"
    launcher.write_bytes(b"launcher")
    called = False

    def run(*_args: Any, **_kwargs: Any) -> object:
        nonlocal called
        called = True
        raise AssertionError("process must not start")

    with pytest.raises(SetupError, match="version"):
        cli._install_runtime(
            bundle,
            tmp_path / "runtime",
            uv_path=uv,
            launcher_path=launcher,
            python_executable=Path(sys.executable),
            expected_version=Version("0.1.0"),
            runner=run,
        )

    assert called is False


def test_runtime_installer_rejects_bare_wheel_before_process(tmp_path: Path) -> None:
    from csaf.setup import cli

    bare = tmp_path / "runtime.whl"
    bare.write_bytes(b"not a bundle")
    uv = tmp_path / "uv.exe"
    uv.write_bytes(b"uv")
    launcher = tmp_path / "csaf.exe"
    launcher.write_bytes(b"launcher")

    with pytest.raises(SetupError, match="runtime bundle"):
        cli._install_runtime(
            bare,
            tmp_path / "runtime",
            uv_path=uv,
            launcher_path=launcher,
            python_executable=Path(sys.executable),
            expected_version=Version("0.1.0"),
            runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        )


def test_runtime_installer_rejects_extra_bundle_member(tmp_path: Path) -> None:
    from csaf.setup import cli

    bundle = _runtime_bundle(tmp_path, extra_member=("unexpected.txt", b"surprise"))
    uv = tmp_path / "uv.exe"
    uv.write_bytes(b"uv")
    launcher = tmp_path / "csaf.exe"
    launcher.write_bytes(b"launcher")

    with pytest.raises(SetupError, match="runtime bundle"):
        cli._install_runtime(
            bundle,
            tmp_path / "runtime",
            uv_path=uv,
            launcher_path=launcher,
            python_executable=Path(sys.executable),
            expected_version=Version("0.1.0"),
            runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        )


def test_runtime_installer_rejects_missing_private_uv_before_process(tmp_path: Path) -> None:
    from csaf.setup import cli

    wheel = tmp_path / "runtime.whl"
    wheel.write_bytes(b"verified")
    launcher = tmp_path / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"launcher")
    called = False

    def run(*_args: Any, **_kwargs: Any) -> object:
        nonlocal called
        called = True
        raise AssertionError("process must not start")

    with pytest.raises(SetupError, match="private runtime bootstrap is unavailable"):
        cli._install_runtime(
            wheel,
            tmp_path / "runtime",
            uv_path=tmp_path / "missing-uv",
            launcher_path=launcher,
            python_executable=Path(sys.executable),
            expected_version=Version("0.1.0"),
            runner=run,
        )

    assert called is False


def test_runtime_installer_translates_process_failure_without_output(
    tmp_path: Path,
) -> None:
    from csaf.setup import cli

    wheel = _runtime_bundle(tmp_path)
    uv = tmp_path / ("uv.exe" if os.name == "nt" else "uv")
    uv.write_bytes(b"uv")
    launcher = tmp_path / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"launcher")
    secret = "tok" + "en-private-value"

    def run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        del _args, _kwargs
        return SimpleNamespace(returncode=1, stderr=secret)

    with pytest.raises(SetupError, match="private runtime installation failed") as raised:
        cli._install_runtime(
            wheel,
            tmp_path / "runtime",
            uv_path=uv,
            launcher_path=launcher,
            python_executable=Path(sys.executable),
            expected_version=Version("0.1.0"),
            runner=run,
        )

    assert secret not in str(raised.value)


def test_make_manager_injects_production_runtime_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    from csaf.setup import cli

    captured: dict[str, Any] = {}

    def manager(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(cli, "SetupManager", manager)
    monkeypatch.setattr(cli, "default_data_root", lambda: Path("C:/CSAF"))
    monkeypatch.setattr(cli, "current_platform", lambda: next(iter(SupportedPlatform)))
    monkeypatch.setattr(cli, "detect_assistants", lambda: ())
    monkeypatch.setattr(cli, "codex_skill_root", lambda: Path("C:/Codex"))

    cli._make_manager()

    assert callable(captured["runtime_installer"])
    assert captured["runtime_installer"].__name__ != "_missing_runtime"


def test_runtime_installer_rejects_incomplete_install(tmp_path: Path) -> None:
    from csaf.setup import cli

    wheel = _runtime_bundle(tmp_path)
    uv = tmp_path / ("uv.exe" if os.name == "nt" else "uv")
    uv.write_bytes(b"uv")
    launcher = tmp_path / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"launcher")
    destination = tmp_path / "runtime"

    def run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        destination.mkdir()
        return SimpleNamespace(returncode=0)

    with pytest.raises(SetupError, match="incomplete"):
        cli._install_runtime(
            wheel,
            destination,
            uv_path=uv,
            launcher_path=launcher,
            python_executable=Path(sys.executable),
            expected_version=Version("0.1.0"),
            runner=run,
        )


def test_runtime_installer_translates_timeout(tmp_path: Path) -> None:
    from csaf.setup import cli

    wheel = _runtime_bundle(tmp_path)
    uv = tmp_path / ("uv.exe" if os.name == "nt" else "uv")
    uv.write_bytes(b"uv")
    launcher = tmp_path / ("csaf.exe" if os.name == "nt" else "csaf")
    launcher.write_bytes(b"launcher")

    def run(arguments: list[str], **_options: Any) -> object:
        raise subprocess.TimeoutExpired(arguments, 120)

    with pytest.raises(SetupError, match="private runtime installation failed"):
        cli._install_runtime(
            wheel,
            tmp_path / "runtime",
            uv_path=uv,
            launcher_path=launcher,
            python_executable=Path(sys.executable),
            expected_version=Version("0.1.0"),
            runner=run,
        )


def test_update_cache_busy_lock_is_nonfatal_and_does_not_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from csaf.setup import cli

    calls = 0
    ticks = iter((0.0, 0.1, 0.2, 0.3))

    def fetch(_source: str | None = None) -> ReleaseManifest:
        nonlocal calls
        calls += 1
        return _manifest()

    monkeypatch.setattr(
        cli,
        "_acquire_activation_lock",
        lambda _path: (_ for _ in ()).throw(SetupError("busy")),
    )
    cache = cli.UpdateCache(
        tmp_path,
        resolver=fetch,
        lock_timeout=0.05,
        monotonic=lambda: next(ticks),
        sleeper=lambda _seconds: None,
    )

    report = cache.check(Version("0.1.0"))

    assert report.offline is True
    assert calls == 0


def test_production_runtime_installer_fails_actionably_without_bootstrap(
    tmp_path: Path,
) -> None:
    from csaf.setup import cli

    wheel = tmp_path / "runtime.whl"
    wheel.write_bytes(b"verified")
    installer = cli._runtime_installer(tmp_path / "CSAF")

    with pytest.raises(SetupError, match="rerun the CSAF installer"):
        installer(wheel, tmp_path / "transaction" / "runtime", Version("0.1.0"))


def _assert_display_has_no_terminal_controls(output: str, *payloads: str) -> None:
    for payload in payloads:
        assert payload not in output
    assert "\x1b" not in output
    assert "\x9b" not in output
    assert "\x9d" not in output
    assert "\u202e" not in output
    for line in output.splitlines():
        assert all(not unicodedata.category(character).startswith("C") for character in line)


def test_install_sanitizes_display_paths_without_changing_operational_plan(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, _ = setup_cli
    payload = "terminal-spoof.invalid"
    injected = (
        f"safe\x1b]8;;https://{payload}/click\x07label\x1b]8;;\x07\x1b[31m\n\x9b32m\u202eevil"
    )
    manager.root = Path("C:/CSAF") / injected
    captured: dict[str, Path] = {}
    original_install = manager.install

    def install(plan: Any, *, consent: object, assume_yes: bool) -> SetupResult:
        captured["runtime"] = plan.runtime_path
        return original_install(plan, consent=consent, assume_yes=assume_yes)

    manager.install = install  # type: ignore[method-assign]
    expected = manager.root / "versions" / "0.2.0"

    result = runner.invoke(cli.setup_app, ["install", "--yes"])

    assert result.exit_code == 0
    _assert_display_has_no_terminal_controls(result.output, payload)
    assert captured["runtime"] == expected
    assert "safe label" in result.output


def test_uninstall_sanitizes_all_recorded_state_paths_before_consent(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, _ = setup_cli
    payload = "recorded-spoof.invalid"
    injected = f"recorded\x1b]8;;https://{payload}/open\x1b\\label\x1b[2J\n\x90ignored\x9c\u202e"
    state = _write_installed_state(manager.root)
    malicious_runtime = Path("C:/versions") / injected
    malicious_office = Path("C:/office") / injected
    malicious_adapter = Path("C:/adapter") / injected
    malicious = state.model_copy(
        update={
            "runtime_paths": {Version("0.1.0"): malicious_runtime},
            "officecli_path": malicious_office,
            "adapter_targets": {AssistantKind.CODEX: malicious_adapter},
        }
    )
    (manager.root / "state.json").write_text(malicious.model_dump_json(), encoding="utf-8")
    (manager.root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_version": "0.1.0",
                "runtime_path": str(malicious_runtime),
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, Path] = {}
    original_uninstall = manager.uninstall

    def uninstall(*, consent: object, include_officecli: bool, assume_yes: bool) -> SetupResult:
        recorded = cli._recorded_state(manager)
        assert recorded is not None
        captured["runtime"] = recorded.runtime_paths[Version("0.1.0")]
        return original_uninstall(
            consent=consent,
            include_officecli=include_officecli,
            assume_yes=assume_yes,
        )

    manager.uninstall = uninstall  # type: ignore[method-assign]
    result = runner.invoke(cli.setup_app, ["uninstall", "--yes"])

    assert result.exit_code == 0
    _assert_display_has_no_terminal_controls(result.output, payload, "ignored")
    assert "recorded label" in result.output
    assert captured["runtime"] == malicious_runtime


def test_setup_status_error_strips_complete_terminal_sequences(
    setup_cli: tuple[Any, FakeManager, FakeResolver],
) -> None:
    cli, manager, _ = setup_cli
    payload = "status-spoof.invalid"
    manager.result = SetupResult(
        SetupStatus.FAILED,
        Version("0.1.0"),
        False,
        error=f"failed \x1b]8;;https://{payload}/open\x07click\x1b]8;;\x07\x1b[2J",
    )

    result = runner.invoke(cli.setup_app, ["install", "--yes"])

    assert result.exit_code == 2
    _assert_display_has_no_terminal_controls(result.output, payload)
    assert "Error: failed click" in result.output
