from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
INSTALLER = ROOT / "installer"
DEPENDENCIES = INSTALLER / "dependencies.json"
SCHEMA = INSTALLER / "release-manifest.schema.json"
RUNTIME_SCHEMA = INSTALLER / "runtime-bundle.schema.json"
MANIFEST = Path(__file__).parent / "fixtures" / "release-manifest.json"
PLATFORMS = {
    "windows-x64",
    "windows-arm64",
    "macos-x64",
    "macos-arm64",
    "linux-x64",
    "linux-arm64",
}


def _dependency_metadata() -> dict[str, object]:
    return json.loads(DEPENDENCIES.read_text(encoding="utf-8"))


def test_dependency_contract_pins_exact_stable_assets() -> None:
    metadata = _dependency_metadata()

    assert metadata["schema_version"] == 1
    assert metadata["python"] == {"version": "3.12.13"}
    assert metadata["uv"]["version"] == "0.12.3"
    assert metadata["officecli"]["version"] == "1.0.143"
    assert metadata["officecli"]["minimum_version"] == "1.0.137"
    assert set(metadata["uv"]["assets"]) == PLATFORMS
    assert set(metadata["officecli"]["assets"]) == PLATFORMS

    for dependency in (metadata["uv"], metadata["officecli"]):
        for platform, asset in dependency["assets"].items():
            assert platform in PLATFORMS
            assert asset["url"].startswith("https://github.com/")
            assert "/main/" not in asset["url"]
            assert len(asset["sha256"]) == 64
            assert set(asset["sha256"]) <= set("0123456789abcdef")
            assert type(asset["size"]) is int and asset["size"] > 0


def test_runtime_bundle_schema_is_strict_for_all_six_platforms() -> None:
    schema = json.loads(RUNTIME_SCHEMA.read_text(encoding="utf-8"))

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["schema_version", "version", "platform", "files"]
    assert schema["properties"]["platform"]["enum"] == sorted(PLATFORMS)
    files = schema["properties"]["files"]
    assert files["required"] == ["requirements.lock"]
    assert "^csaf-" in "".join(files["patternProperties"])
    assert files["additionalProperties"] is False


def test_release_manifest_schema_is_strict_and_requires_all_assets() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "schema_version",
        "version",
        "runtime",
        "codex_skill",
        "claude_plugin",
        "officecli",
    ]
    assert schema["$defs"]["platformAssets"]["required"] == sorted(PLATFORMS)
    assert schema["$defs"]["asset"]["properties"]["url"]["pattern"] == "^https://"
    assert schema["$defs"]["asset"]["properties"]["sha256"]["pattern"] == "^[0-9a-f]{64}$"


@pytest.mark.parametrize("name", ["install.ps1", "install.sh"])
def test_bootstrap_source_is_consent_first_and_never_executes_downloaded_text(name: str) -> None:
    source = (INSTALLER / name).read_text(encoding="utf-8")
    lowered = source.lower()

    assert "officecli 1.0.143" in lowered
    assert "uv 0.12.3" in lowered
    assert "python 3.12.13" in lowered
    assert "mandatory" in lowered
    assert "powerpoint and word" in lowered
    assert "no api key" in lowered
    assert "--yes" in source
    assert "--codex-only" in source
    assert "--claude-only" in source
    assert "csaf.setup.cli" in source
    assert "UV_UNMANAGED_INSTALL" in source
    assert "UV_PYTHON_INSTALL_DIR" in source
    assert "UV_CACHE_DIR" in source
    assert "UV_OFFLINE" in source
    urls = re.findall(r"""https://[^"' ]+""", source)
    assert urls and all("/main/" not in url for url in urls)
    assert "invoke-expression" not in lowered
    assert "\niex " not in lowered
    assert "eval " not in lowered
    assert "sh -c" not in lowered
    assert "| sh" not in lowered
    assert "| bash" not in lowered
    assert "\nassert " not in source
    if name == "install.sh":
        assert source.count('"$python_executable" -I -S -c') == 2


def test_powershell_uses_one_manual_https_redirect_boundary() -> None:
    source = (INSTALLER / "install.ps1").read_text(encoding="utf-8")

    assert source.count("Invoke-WebRequest") == 1
    assert "function Invoke-HttpsManualRedirect" in source
    assert "-MaximumRedirection 0" in source
    assert "userinfo" in source.lower()
    assert "redirect limit" in source.lower()
    assert "CSAF_INSTALLER_LIBRARY_ONLY" not in source
    assert source.count('$ProgressPreference = "SilentlyContinue"') == 1


def _redirect_wrapper(
    tmp_path: Path, locations: list[str], payload: bytes
) -> subprocess.CompletedProcess[str]:
    executable = _powershell()
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    encoded_locations = [base64.b64encode(value.encode()).decode() for value in locations]
    encoded_payload = base64.b64encode(payload).decode()
    location_array = ",".join(f'"{value}"' for value in encoded_locations)
    wrapper = tmp_path / "redirect-test.ps1"
    wrapper.write_text(
        f'''$script:Locations = @({location_array})
$script:Calls = 0
$script:Payload = [Convert]::FromBase64String("{encoded_payload}")
function global:Invoke-WebRequest {{
    param([switch]$UseBasicParsing, $Uri, $TimeoutSec, $MaximumRedirection, $Headers)
    $index = $script:Calls
    $script:Calls += 1
    if ($index -lt $script:Locations.Count) {{
        $location = [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($script:Locations[$index])
        )
        return [pscustomobject]@{{StatusCode=302; Headers=@{{Location=$location}}; Content=""}}
    }}
    return [pscustomobject]@{{StatusCode=200; Headers=@{{}}; Content=$script:Payload}}
}}
. "{(INSTALLER / "install.ps1").as_posix()}"
try {{
    $bytes = Invoke-HttpsManualRedirect "https://releases.test/start" 5 1024
    $report = [pscustomobject]@{{
        ok=$true; calls=$script:Calls; payload=[Convert]::ToBase64String($bytes)
    }}
    $report | ConvertTo-Json -Compress
    exit 0
}} catch {{
    $report = [pscustomobject]@{{
        ok=$false; calls=$script:Calls; error=$_.Exception.Message
    }}
    $report | ConvertTo-Json -Compress
    exit 3
}}
''',
        encoding="utf-8",
    )
    return subprocess.run(
        [executable, "-NoProfile", "-File", str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )


def test_powershell_redirect_helper_allows_https_cross_origin_signed_payload(
    tmp_path: Path,
) -> None:
    payload = b"manifest-or-binary-payload"
    result = _redirect_wrapper(
        tmp_path,
        ["https://objects.example.test/final?signature=abc123"],
        payload,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "ok": True,
        "calls": 2,
        "payload": base64.b64encode(payload).decode(),
    }


@pytest.mark.parametrize(
    "location",
    [
        "http://objects.example.test/file",
        "https://user:password@objects.example.test/file",
        "/relative/file",
        "not a URI",
    ],
)
def test_powershell_redirect_helper_rejects_unsafe_location_before_next_request(
    tmp_path: Path, location: str
) -> None:
    result = _redirect_wrapper(tmp_path, [location], b"must-not-be-returned")

    assert result.returncode == 3
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["calls"] == 1
    assert "HTTPS redirect" in report["error"]


def test_powershell_redirect_helper_rejects_loop_without_leaking_url(tmp_path: Path) -> None:
    result = _redirect_wrapper(tmp_path, ["https://releases.test/start"], b"unused")

    assert result.returncode == 3
    report = json.loads(result.stdout)
    assert report["calls"] == 1
    assert report["error"] == "HTTPS redirect loop detected"
    assert "releases.test" not in result.stdout


def test_powershell_redirect_helper_enforces_limit_without_leaking_signed_query(
    tmp_path: Path,
) -> None:
    locations = [
        f"https://objects.example.test/hop-{index}?signature=secret-{index}" for index in range(9)
    ]
    result = _redirect_wrapper(tmp_path, locations, b"unused")

    assert result.returncode == 3
    report = json.loads(result.stdout)
    assert report["calls"] == 9
    assert report["error"] == "HTTPS redirect limit exceeded"
    assert "signature" not in result.stdout
    assert "secret" not in result.stdout


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def test_powershell_dry_run_is_offline_and_has_no_filesystem_effects(tmp_path: Path) -> None:
    executable = _powershell()
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    data_root = tmp_path / "CSAF dry run"
    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-File",
            str(INSTALLER / "install.ps1"),
            "--dry-run",
            "--yes",
            "--platform",
            "windows-x64",
            "--manifest",
            str(MANIFEST),
            "--data-root",
            str(data_root),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env={**os.environ, "CSAF_INSTALLER_NETWORK_FORBIDDEN": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert "Platform: windows-x64" in result.stdout
    assert "OfficeCLI 1.0.143" in result.stdout
    assert "Targets:" in result.stdout
    assert str(data_root) in result.stdout
    assert not Path(data_root).exists()


def test_powershell_whatif_is_an_offline_no_write_dry_run(tmp_path: Path) -> None:
    executable = _powershell()
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    data_root = tmp_path / "CSAF what-if"
    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-File",
            str(INSTALLER / "install.ps1"),
            "-WhatIf",
            "-Platform",
            "windows-x64",
            "-ManifestPath",
            str(MANIFEST),
            "-DataRoot",
            str(data_root),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env={**os.environ, "CSAF_INSTALLER_NETWORK_FORBIDDEN": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert "Dry run complete" in result.stdout
    assert not data_root.exists()


def test_shell_syntax_and_dry_run_are_offline() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    syntax = subprocess.run(
        [bash, "-n", "installer/install.sh"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert syntax.returncode == 0, syntax.stderr

    data_root = f"/tmp/csaf-dry-run-{uuid.uuid4().hex}"
    manifest = "tests/setup/fixtures/release-manifest.json" if os.name == "nt" else str(MANIFEST)
    result = subprocess.run(
        [
            bash,
            "installer/install.sh",
            "--dry-run",
            "--yes",
            "--platform",
            "linux-x64",
            "--manifest",
            manifest,
            "--data-root",
            str(data_root),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env={**os.environ, "CSAF_INSTALLER_NETWORK_FORBIDDEN": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert "Platform: linux-x64" in result.stdout
    assert "OfficeCLI 1.0.143" in result.stdout
    assert "Targets:" in result.stdout
    assert str(data_root) in result.stdout
    if os.name == "nt":
        absent = subprocess.run([bash, "-c", f"test ! -e {data_root}"], check=False)
        assert absent.returncode == 0
    else:
        assert not Path(data_root).exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-root",
        "nested-extra",
        "boolean-size",
        "wrong-runtime-shape",
        "duplicate-key",
        "escaped-string",
        "trailing-garbage",
    ],
)
def test_shell_strictly_rejects_invalid_manifest_before_plan_or_writes(
    tmp_path: Path, mutation: str
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if mutation == "extra-root":
        payload["unexpected"] = "must be rejected"
    elif mutation == "nested-extra":
        payload["runtime"]["linux-x64"]["unexpected"] = "must be rejected"
    elif mutation == "boolean-size":
        payload["runtime"]["linux-x64"]["size"] = True
    elif mutation == "wrong-runtime-shape":
        payload["runtime"] = "windows-x64 windows-arm64 macos-x64 macos-arm64 linux-x64 linux-arm64"
    manifest_text = json.dumps(payload, indent=2)
    if mutation == "duplicate-key":
        manifest_text = manifest_text.replace(
            '"version": "0.1.0",',
            '"version": "0.1.0",\n  "version": "0.1.0",',
            1,
        )
    elif mutation == "escaped-string":
        manifest_text = manifest_text.replace("https://", r"https:\/\/", 1)
    elif mutation == "trailing-garbage":
        manifest_text += "\nnot-json"
    manifest = tmp_path / f"{mutation}.json"
    manifest.write_text(manifest_text, encoding="utf-8")
    data_root = f"/tmp/csaf-invalid-{uuid.uuid4().hex}"
    manifest_arg = manifest.as_posix()
    if os.name == "nt":
        manifest_arg = f"/mnt/{manifest.drive[0].lower()}/{manifest_arg[3:]}"

    result = subprocess.run(
        [
            bash,
            "installer/install.sh",
            "--dry-run",
            "--yes",
            "--platform",
            "linux-x64",
            "--manifest",
            manifest_arg,
            "--data-root",
            data_root,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env={**os.environ, "CSAF_INSTALLER_NETWORK_FORBIDDEN": "1"},
    )

    assert result.returncode == 2
    assert "release manifest is invalid" in result.stderr
    assert "installation plan" not in result.stdout
    absent = subprocess.run([bash, "-c", f'test ! -e "{data_root}"'], check=False)
    assert absent.returncode == 0


def test_powershell_declined_consent_has_no_side_effects(tmp_path: Path) -> None:
    executable = _powershell()
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    data_root = tmp_path / "declined"
    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-File",
            str(INSTALLER / "install.ps1"),
            "--platform",
            "windows-x64",
            "--manifest",
            str(MANIFEST),
            "--data-root",
            str(data_root),
        ],
        cwd=ROOT,
        input="n\n",
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env={**os.environ, "CSAF_INSTALLER_NETWORK_FORBIDDEN": "1"},
    )

    assert result.returncode == 2
    assert "installation was declined" in result.stderr
    assert not Path(data_root).exists()


def test_shell_declined_consent_has_no_side_effects() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    data_root = f"/tmp/csaf-declined-{uuid.uuid4().hex}"
    manifest = "tests/setup/fixtures/release-manifest.json" if os.name == "nt" else str(MANIFEST)
    result = subprocess.run(
        [
            bash,
            "installer/install.sh",
            "--platform",
            "linux-x64",
            "--manifest",
            manifest,
            "--data-root",
            str(data_root),
        ],
        cwd=ROOT,
        input="n\n",
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env={**os.environ, "CSAF_INSTALLER_NETWORK_FORBIDDEN": "1"},
    )

    assert result.returncode == 2
    assert "installation was declined" in result.stderr
    if os.name == "nt":
        absent = subprocess.run([bash, "-c", f"test ! -e {data_root}"], check=False)
        assert absent.returncode == 0
    else:
        assert not Path(data_root).exists()


def test_bootstraps_keep_private_python_and_only_clean_exact_staging_directory() -> None:
    powershell = (INSTALLER / "install.ps1").read_text(encoding="utf-8")
    shell = (INSTALLER / "install.sh").read_text(encoding="utf-8")

    for source in (powershell, shell):
        assert "bin" in source
        assert "python" in source
        assert "cache" in source
        assert "bootstrap" in source
        assert "staging" in source
        assert "--offline" in source
        assert "--no-config" in source
        assert "--no-index" in source
        assert "--require-hashes" in source
        assert "--find-links" in source
        assert "runtime-bundle.json" in source

    assert "Remove-Item -LiteralPath $StagingDirectory" in powershell
    assert 'rm -rf -- "$staging_directory"' in shell
    assert "\numask 077\nensure_private_data_root\nensure_private_directory" in shell


def test_bootstraps_propagate_the_validated_data_root_to_native_setup() -> None:
    powershell = (INSTALLER / "install.ps1").read_text(encoding="utf-8")
    shell = (INSTALLER / "install.sh").read_text(encoding="utf-8")

    assert "$env:CSAF_DATA_ROOT = $DataRoot" in powershell
    assert 'export CSAF_DATA_ROOT="$data_root"' in shell
    assert powershell.index("$env:CSAF_DATA_ROOT = $DataRoot") < powershell.index(
        '"-m", "csaf.setup.cli", "install"'
    )
    assert shell.index('export CSAF_DATA_ROOT="$data_root"') < shell.index(
        "-m csaf.setup.cli install"
    )


def test_bootstrap_downloads_are_streamed_with_hard_transfer_limits() -> None:
    powershell = (INSTALLER / "install.ps1").read_text(encoding="utf-8")
    shell = (INSTALLER / "install.sh").read_text(encoding="utf-8")

    assert "ResponseHeadersRead" in powershell
    assert "Invoke-HttpsBoundedDownload $Source 30 1048576" in powershell
    assert ".Read($buffer, 0, $buffer.Length)" in powershell
    assert "CopyTo($memory)" not in powershell
    assert "ToArray()" not in powershell
    assert '--max-filesize "$expected_size"' in shell


def test_bootstraps_reject_linked_roots_and_enforce_private_root_permissions() -> None:
    powershell = (INSTALLER / "install.ps1").read_text(encoding="utf-8")
    shell = (INSTALLER / "install.sh").read_text(encoding="utf-8")

    assert "function Initialize-PrivateDataRoot" in powershell
    assert "ReparsePoint" in powershell
    assert "Initialize-PrivateDataRoot $DataRoot" in powershell
    assert "data root must not be a filesystem root" in powershell
    assert "ensure_private_data_root" in shell
    assert '[ -L "$component" ]' in shell
    assert 'chmod 700 "$data_root"' in shell
    assert '[ "$data_root" != "/" ]' in shell
    assert shell.index("ensure_private_data_root") < shell.index(
        'ensure_private_directory "$data_root/staging"'
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink boundary")
def test_shell_rejects_symlinked_data_root_before_any_outside_write(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        [
            bash,
            "installer/install.sh",
            "--yes",
            "--platform",
            "linux-x64",
            "--manifest",
            str(MANIFEST),
            "--data-root",
            str(linked),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env={**os.environ, "CSAF_INSTALLER_NETWORK_FORBIDDEN": "1"},
    )

    assert result.returncode == 2
    assert "symbolic link" in result.stderr
    assert list(outside.iterdir()) == []


def test_bootstraps_validate_every_controlled_child_directory_before_use() -> None:
    powershell = (INSTALLER / "install.ps1").read_text(encoding="utf-8")
    shell = (INSTALLER / "install.sh").read_text(encoding="utf-8")

    assert "function Initialize-PrivateDirectory" in powershell
    assert "function Assert-SafeFileTarget" in powershell
    assert "Assert-SafeFileTarget $UvPath $true" in powershell
    assert "Assert-SafeFileTarget $ManifestFile $false" in powershell
    for child in ("staging", "bin", "python", "cache", "cache\\uv"):
        assert f'Initialize-PrivateDirectory (Join-Path $DataRoot "{child}")' in powershell
    assert "ensure_private_directory() {" in shell
    assert "safe_file_target() {" in shell
    assert 'safe_file_target "$uv_path" 1' in shell
    assert 'safe_file_target "$destination" 0' in shell
    for child in ("staging", "bin", "python", "cache", "cache/uv"):
        assert f'ensure_private_directory "$data_root/{child}"' in shell
    assert 'mktemp -d "$data_root/staging/bootstrap-XXXXXXXX"' in shell


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink boundary")
def test_shell_rejects_symlinked_controlled_child_before_outside_write(
    tmp_path: Path,
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    data_root = tmp_path / "data"
    outside = tmp_path / "outside"
    data_root.mkdir()
    outside.mkdir()
    (data_root / "staging").symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        [
            bash,
            "installer/install.sh",
            "--yes",
            "--platform",
            "linux-x64",
            "--manifest",
            str(MANIFEST),
            "--data-root",
            str(data_root),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env={**os.environ, "CSAF_INSTALLER_NETWORK_FORBIDDEN": "1"},
    )

    assert result.returncode == 2
    assert "installer-controlled directory is unsafe" in result.stderr
    assert list(outside.iterdir()) == []


def _run_powershell_library(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    executable = _powershell()
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    wrapper = tmp_path / "library-test.ps1"
    wrapper.write_text(
        f'. "{(INSTALLER / "install.ps1").as_posix()}"\n{body}',
        encoding="utf-8",
    )
    return subprocess.run(
        [executable, "-NoProfile", "-File", str(wrapper)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_powershell_tightens_permissive_root_acl_before_child_writes(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "acl-root"
    data_root.mkdir()
    body = f'''
$root = "{data_root.as_posix()}"
$acl = Get-Acl -LiteralPath $root
$everyone = [Security.Principal.SecurityIdentifier]::new("S-1-1-0")
$rule = [Security.AccessControl.FileSystemAccessRule]::new(
    $everyone, [Security.AccessControl.FileSystemRights]::FullControl,
    [Security.AccessControl.AccessControlType]::Allow
)
[void]$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $root -AclObject $acl
Initialize-PrivateDataRoot $root
$verified = Get-Acl -LiteralPath $root
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$sids = @($verified.Access | ForEach-Object {{
    $_.IdentityReference.Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
}})
[pscustomobject]@{{
    owner=([Security.Principal.NTAccount]$verified.Owner).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    current=$current
    protected=$verified.AreAccessRulesProtected
    sids=$sids
    children=@(Get-ChildItem -LiteralPath $root).Count
}} | ConvertTo-Json -Compress
'''
    result = _run_powershell_library(tmp_path, body)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["owner"] == report["current"]
    assert report["protected"] is True
    assert set(report["sids"]) <= {report["current"], "S-1-5-18", "S-1-5-32-544"}
    assert "S-1-1-0" not in report["sids"]
    assert report["children"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point contract")
def test_powershell_rejects_junction_child_before_outside_write(tmp_path: Path) -> None:
    data_root = tmp_path / "junction-root"
    outside = tmp_path / "outside"
    outside.mkdir()
    body = f'''
$root = "{data_root.as_posix()}"
$outside = "{outside.as_posix()}"
Initialize-PrivateDataRoot $root
New-Item -ItemType Junction -Path (Join-Path $root "staging") -Target $outside |
    Out-Null
try {{
    Initialize-PrivateDirectory (Join-Path $root "staging")
    exit 9
}} catch {{
    if (@(Get-ChildItem -LiteralPath $outside).Count -ne 0) {{ exit 8 }}
    exit 0
}}
'''
    result = _run_powershell_library(tmp_path, body)

    assert result.returncode == 0, result.stderr
    assert list(outside.iterdir()) == []


def test_powershell_private_root_uses_verified_owner_only_acl() -> None:
    powershell = (INSTALLER / "install.ps1").read_text(encoding="utf-8")

    assert "[Security.Principal.WindowsIdentity]::GetCurrent().User" in powershell
    assert "SetAccessRuleProtection($true, $false)" in powershell
    assert "[Security.AccessControl.FileSystemAccessRule]" in powershell
    assert "S-1-1-0" in powershell
    assert "private ACL" in powershell
    assert powershell.index("Set-Acl") < powershell.index(
        'Initialize-PrivateDirectory (Join-Path $DataRoot "staging")'
    )


def test_powershell_51_stages_manifest_as_bom_free_utf8() -> None:
    powershell = (INSTALLER / "install.ps1").read_text(encoding="utf-8")

    assert "[Text.UTF8Encoding]::new($false)" in powershell
    assert "[IO.File]::WriteAllText(" in powershell
    assert "$ManifestFile, $ManifestJson" in powershell
    assert "Set-Content -LiteralPath $ManifestFile -Encoding UTF8" not in powershell


def test_powershell_dry_run_rejects_boolean_asset_size_before_writes(tmp_path: Path) -> None:
    executable = _powershell()
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["runtime"]["windows-x64"]["size"] = True
    manifest = tmp_path / "invalid.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    data_root = tmp_path / "must-not-exist"

    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-File",
            str(INSTALLER / "install.ps1"),
            "--dry-run",
            "--yes",
            "--platform",
            "windows-x64",
            "--manifest",
            str(manifest),
            "--data-root",
            str(data_root),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env={**os.environ, "CSAF_INSTALLER_NETWORK_FORBIDDEN": "1"},
    )

    assert result.returncode == 2
    assert "release manifest is invalid" in result.stderr
    assert not data_root.exists()


def test_test_release_manifest_is_strict_and_tag_pinned() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["version"] == "0.1.0"
    assert set(manifest["runtime"]) == PLATFORMS
    assert set(manifest["officecli"]["assets"]) == PLATFORMS
    for mapping in (manifest["runtime"], manifest["officecli"]["assets"]):
        for asset in mapping.values():
            assert "/download/v" in asset["url"]
            assert "/main/" not in asset["url"]
            if mapping is manifest["runtime"]:
                assert asset["url"].endswith(".zip")


def _embedded_bundle_validator(name: str) -> str:
    source = (INSTALLER / name).read_text(encoding="utf-8")
    pattern = (
        r'FromBase64String\("([A-Za-z0-9+/=]+)"\)'
        if name == "install.ps1"
        else r"base64\.b64decode\('([A-Za-z0-9+/=]+)'\)"
    )
    match = re.search(pattern, source)
    assert match is not None
    validator = base64.b64decode(match.group(1)).decode("utf-8")
    assert "assert " not in validator
    if name == "install.ps1":
        assert '"-I", "-S", "-c", $BundleValidator' in source
    else:
        assert '"$python_executable" -I -S -c' in source
    return validator


def _write_bootstrap_bundle(
    path: Path,
    *,
    mutation: str | None = None,
    version: str = "1.2.3",
) -> None:
    runtime = b"dummy-runtime-wheel"
    runtime_name = f"csaf-{version}-py3-none-any.whl"
    dependency = b"dummy-dependency-wheel"
    dependency_name = (
        "wheelhouse/../../escaped.whl"
        if mutation == "traversal"
        else "wheelhouse/dummy_dependency-1.0.0-py3-none-any.whl"
    )
    lock = (
        f"./{runtime_name} "
        f"--hash=sha256:{hashlib.sha256(runtime).hexdigest()}\n"
        f"dummy-dependency==1.0.0 --hash=sha256:{hashlib.sha256(dependency).hexdigest()}\n"
    ).encode()
    if mutation == "lock-option":
        lock = b"--extra-index-url https://registry.example/simple\n"
    payloads = {
        runtime_name: runtime,
        "requirements.lock": lock,
        dependency_name: dependency,
    }
    if mutation == "extra":
        payloads["unexpected.txt"] = b"rejected"
    manifest = {
        "schema_version": 1,
        "version": version,
        "platform": "linux-x64",
        "files": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for name, data in payloads.items()
        },
    }
    if mutation == "hash":
        manifest["files"][runtime_name]["sha256"] = "0" * 64
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("runtime-bundle.json", json.dumps(manifest))
        for member_name, data in payloads.items():
            archive.writestr(member_name, data)


def _run_embedded_validator(
    validator: str,
    archive: Path,
    destination: Path,
    shadow: Path,
    *,
    expected_version: str = "1.2.3",
) -> subprocess.CompletedProcess[str]:
    shadow.mkdir(exist_ok=True)
    (shadow / "hashlib.py").write_text("raise RuntimeError('import hijacked')\n", encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-O",
            "-I",
            "-S",
            "-c",
            validator,
            str(archive),
            str(destination),
            "linux-x64",
            expected_version,
        ],
        cwd=shadow,
        env={**os.environ, "PYTHONOPTIMIZE": "1", "PYTHONPATH": str(shadow)},
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("name", ["install.ps1", "install.sh"])
def test_embedded_bundle_validator_accepts_valid_bundle_when_python_is_optimized_and_isolated(
    tmp_path: Path, name: str
) -> None:
    validator = _embedded_bundle_validator(name)
    archive = tmp_path / f"{name}-valid.zip"
    destination = tmp_path / f"{name}-valid"
    _write_bootstrap_bundle(archive)

    result = _run_embedded_validator(validator, archive, destination, tmp_path / f"{name}-shadow")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["install.ps1", "install.sh"])
@pytest.mark.parametrize("mutation", ["extra", "hash", "lock-option", "traversal"])
def test_embedded_bundle_validator_rejects_malicious_bundle_when_python_is_optimized(
    tmp_path: Path, name: str, mutation: str
) -> None:
    validator = _embedded_bundle_validator(name)
    archive = tmp_path / f"{name}-{mutation}.zip"
    destination = tmp_path / f"{name}-{mutation}"
    _write_bootstrap_bundle(archive, mutation=mutation)

    result = _run_embedded_validator(
        validator, archive, destination, tmp_path / f"{name}-{mutation}-shadow"
    )

    assert result.returncode != 0
    assert not destination.exists()
    assert not (tmp_path / "escaped.whl").exists()


@pytest.mark.parametrize("name", ["install.ps1", "install.sh"])
def test_embedded_bundle_validator_rejects_inner_outer_version_mismatch(
    tmp_path: Path, name: str
) -> None:
    validator = _embedded_bundle_validator(name)
    archive = tmp_path / f"{name}-version.zip"
    destination = tmp_path / f"{name}-version"
    _write_bootstrap_bundle(archive, version="9.9.9")

    result = _run_embedded_validator(
        validator,
        archive,
        destination,
        tmp_path / f"{name}-version-shadow",
        expected_version="1.2.3",
    )

    assert result.returncode != 0
    assert not destination.exists()


def test_powershell_library_bypass_environment_cannot_skip_normal_entrypoint(
    tmp_path: Path,
) -> None:
    executable = _powershell()
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    data_root = tmp_path / "must-not-exist"

    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-File",
            str(INSTALLER / "install.ps1"),
            "--dry-run",
            "--yes",
            "--platform",
            "windows-x64",
            "--manifest",
            str(MANIFEST),
            "--data-root",
            str(data_root),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "CSAF_INSTALLER_LIBRARY_ONLY": "1",
            "CSAF_INSTALLER_NETWORK_FORBIDDEN": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "Dry run complete" in result.stdout
    assert not data_root.exists()
