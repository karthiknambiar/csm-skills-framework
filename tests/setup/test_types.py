from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from csaf.setup import (
    AssistantKind,
    InstallState,
    OfficeCLIDependency,
    ReleaseAsset,
    ReleaseManifest,
    SupportedPlatform,
    Version,
)


def _asset() -> dict[str, object]:
    return {
        "url": "https://example.test/file",
        "sha256": "a" * 64,
        "size": 1,
    }


def _platform_assets() -> dict[str, dict[str, object]]:
    return {platform.value: _asset() for platform in SupportedPlatform}


def _valid_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "version": "0.1.0",
        "runtime": _platform_assets(),
        "codex_skill": _asset(),
        "claude_plugin": _asset(),
        "officecli": {
            "version": "1.0.143",
            "minimum_version": "1.0.137",
            "assets": _platform_assets(),
        },
    }


def test_release_manifest_requires_https_hashes_and_supported_assets() -> None:
    manifest = ReleaseManifest.model_validate(_valid_manifest())
    assert manifest.version == Version("0.1.0")
    assert manifest.officecli.minimum_version == Version("1.0.137")
    assert set(manifest.officecli.assets) == set(SupportedPlatform)


@pytest.mark.parametrize("url", ["http://example.test/file", "file:///tmp/file"])
def test_release_asset_rejects_non_https_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        ReleaseAsset(url=url, sha256="a" * 64, size=1)


def test_release_asset_rejects_coerced_size() -> None:
    with pytest.raises(ValidationError):
        ReleaseAsset(url="https://example.test/file", sha256="a" * 64, size="1")


def test_install_state_rejects_coerced_officecli_ownership() -> None:
    with pytest.raises(ValidationError):
        InstallState(officecli_installed_by_csaf="false")


@pytest.mark.parametrize("model", [ReleaseManifest, InstallState])
def test_schema_version_rejects_boolean(model: type[ReleaseManifest] | type[InstallState]) -> None:
    payload = _valid_manifest() if model is ReleaseManifest else {}
    payload["schema_version"] = True

    with pytest.raises(ValidationError):
        model.model_validate(payload)

@pytest.mark.parametrize("mapping", ["runtime", "officecli"])
def test_release_manifest_requires_every_supported_platform(mapping: str) -> None:
    payload = _valid_manifest()
    target = payload["runtime"] if mapping == "runtime" else payload["officecli"]["assets"]
    target.pop(SupportedPlatform.LINUX_ARM64.value)

    with pytest.raises(ValidationError):
        ReleaseManifest.model_validate(payload)


def test_release_models_reject_unknown_fields() -> None:
    asset = _asset() | {"signature": "unexpected"}
    with pytest.raises(ValidationError):
        ReleaseAsset.model_validate(asset)

    dependency = {
        "version": "1.0.143",
        "minimum_version": "1.0.137",
        "assets": _platform_assets(),
        "channel": "stable",
    }
    with pytest.raises(ValidationError):
        OfficeCLIDependency.model_validate(dependency)

    manifest = _valid_manifest() | {"channel": "stable"}
    with pytest.raises(ValidationError):
        ReleaseManifest.model_validate(manifest)


@pytest.mark.parametrize("value", ["1", "1.2", "1.2.3.4", "1.2.-1", "v1.2.3"])
def test_version_requires_exactly_three_non_negative_components(value: str) -> None:
    with pytest.raises(ValueError, match="three non-negative integer components"):
        Version(value)


def test_version_orders_components_numerically() -> None:
    assert Version("1.2.9") < Version("1.10.0")


def test_version_equality_uses_numeric_components() -> None:
    assert Version("01.2.3") == Version("1.2.3")


def test_models_accept_typed_versions_in_every_version_field(tmp_path: Path) -> None:
    version = Version("1.2.3")
    asset = ReleaseAsset.model_validate(_asset())
    assets = {platform: asset for platform in SupportedPlatform}
    officecli = OfficeCLIDependency(
        version=version,
        minimum_version=version,
        assets=assets,
    )

    manifest = ReleaseManifest(
        schema_version=1,
        version=version,
        runtime=assets,
        codex_skill=asset,
        claude_plugin=asset,
        officecli=officecli,
    )
    state = InstallState(
        active_version=version,
        installed_versions=[version],
        runtime_paths={version: tmp_path},
        officecli_version=version,
    )

    assert manifest.version is version
    assert manifest.officecli.version is version
    assert manifest.officecli.minimum_version is version
    assert state.active_version is version
    assert state.installed_versions == (version,)
    assert state.runtime_paths == {version: tmp_path}
    assert state.officecli_version is version


def test_validated_mappings_reject_mutation(tmp_path: Path) -> None:
    manifest = ReleaseManifest.model_validate(_valid_manifest())
    state = InstallState(
        runtime_paths={"0.1.0": tmp_path},
        verified_checksums={"runtime": "b" * 64},
        adapter_targets={AssistantKind.CODEX: tmp_path / "codex"},
    )
    mappings_and_keys = [
        (manifest.runtime, SupportedPlatform.WINDOWS_X64),
        (manifest.officecli.assets, SupportedPlatform.WINDOWS_X64),
        (state.runtime_paths, Version("0.1.0")),
        (state.verified_checksums, "runtime"),
        (state.adapter_targets, AssistantKind.CODEX),
    ]

    for mapping, key in mappings_and_keys:
        with pytest.raises(TypeError):
            mapping[key] = mapping[key]


def test_immutable_mappings_preserve_json_round_trips(tmp_path: Path) -> None:
    manifest = ReleaseManifest.model_validate(_valid_manifest())
    state = InstallState(
        runtime_paths={"0.1.0": tmp_path},
        verified_checksums={"runtime": "b" * 64},
        adapter_targets={AssistantKind.CODEX: tmp_path / "codex"},
    )

    assert ReleaseManifest.model_validate_json(manifest.model_dump_json()) == manifest
    assert InstallState.model_validate_json(state.model_dump_json()) == state

def test_install_state_round_trips_without_customer_or_secret_fields(tmp_path: Path) -> None:
    state = InstallState(
        active_version="0.1.0",
        installed_versions=["0.1.0"],
        runtime_paths={"0.1.0": tmp_path / "versions" / "0.1.0"},
        verified_checksums={"runtime": "b" * 64},
        adapter_targets={AssistantKind.CODEX: tmp_path / "codex"},
        officecli_version="1.0.143",
        officecli_path=tmp_path / "officecli" / "1.0.143" / "officecli",
        officecli_sha256="c" * 64,
        officecli_installed_by_csaf=True,
        installed_at=datetime(2026, 8, 11, tzinfo=UTC),
        updated_at=datetime(2026, 8, 11, tzinfo=UTC),
    )
    payload = state.model_dump_json()
    assert "customer" not in payload.casefold()
    assert "token" not in payload.casefold()
    assert InstallState.model_validate_json(payload) == state


def test_install_state_rejects_unknown_or_secret_fields() -> None:
    with pytest.raises(ValidationError):
        InstallState(active_version="0.1.0", token="secret")
