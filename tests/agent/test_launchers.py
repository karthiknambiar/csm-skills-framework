from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "csaf" / "skills" / "csaf" / "scripts"
VERSION = "0.1.0"
OFFICE_VERSION = "1.0.143"


def _state(runtime: str, officecli: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "active_version": VERSION,
        "installed_versions": [VERSION],
        "runtime_paths": {VERSION: runtime},
        "verified_checksums": {},
        "adapter_targets": {},
        "officecli_version": OFFICE_VERSION,
        "officecli_path": officecli,
        "officecli_sha256": "a" * 64,
        "officecli_installed_by_csaf": True,
        "installed_at": "2026-08-11T00:00:00Z",
        "updated_at": "2026-08-11T00:00:00Z",
    }


def _write_metadata(root: Path, runtime: str, officecli: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_version": VERSION,
                "runtime_path": runtime,
            }
        ),
        encoding="utf-8",
    )
    (root / "state.json").write_text(json.dumps(_state(runtime, officecli)), encoding="utf-8")


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is not installed")
    return executable


def _posix_tools() -> tuple[str, object]:
    if os.name == "nt":
        shell = Path("C:/Program Files/Git/bin/bash.exe")
        cygpath = Path("C:/Program Files/Git/usr/bin/cygpath.exe")
        if not shell.is_file() or not cygpath.is_file():
            pytest.skip("Git Bash is not installed")

        def posix(path: Path) -> str:
            return subprocess.check_output([str(cygpath), "-u", str(path)], text=True).strip()

        return str(shell), posix
    executable = shutil.which("bash")
    if executable is None:
        pytest.skip("bash is not installed")
    return executable, str


def _assert_bootstrap_request(completed: subprocess.CompletedProcess[str], platform: str) -> None:
    assert completed.returncode == 3
    report = json.loads(completed.stderr.strip().splitlines()[-1])
    suffix = "ps1" if platform == "windows" else "sh"
    assert report == {
        "status": "bootstrap_required",
        "reason": "runtime_missing_or_unhealthy",
        "next_action": "run_platform_bootstrap_after_explicit_consent",
        "requires_consent": True,
        "installs": ["CSAF", "OfficeCLI"],
        "network": "verified tagged stable release assets over HTTPS",
        "api_key_required": False,
        "hosted_ai": False,
        "bootstrap": {
            "url": (
                "https://github.com/karthiknambiar/csm-skills-framework/"
                f"releases/latest/download/install.{suffix}"
            ),
            "invocation": (
                "powershell -NoProfile -ExecutionPolicy Bypass -File <downloaded-install.ps1>"
                if platform == "windows"
                else "sh <downloaded-install.sh>"
            ),
        },
    }
    assert "csaf setup install" not in report["bootstrap"]["invocation"]


def test_powershell_launcher_reports_reachable_bootstrap_when_runtime_missing(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env["CSAF_DATA_ROOT"] = str(tmp_path / "missing")
    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-File", str(SCRIPTS / "csaf.ps1"), "doctor"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    _assert_bootstrap_request(completed, "windows")


def test_posix_launcher_reports_reachable_bootstrap_when_runtime_missing(
    tmp_path: Path,
) -> None:
    shell, posix = _posix_tools()
    env = os.environ.copy()
    env["CSAF_DATA_ROOT"] = posix(tmp_path / "missing")
    completed = subprocess.run(
        [shell, posix(SCRIPTS / "csaf.sh"), "doctor"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    _assert_bootstrap_request(completed, "posix")


def test_powershell_launcher_preserves_argv_and_json_stdout(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    runtime = data_root / "versions" / VERSION
    runtime.mkdir(parents=True)
    shutil.copy2(sys._base_executable, runtime / "csaf.exe")
    officecli = data_root / "officecli" / OFFICE_VERSION / "officecli.exe"
    officecli.parent.mkdir(parents=True)
    officecli.write_bytes(b"fixture")
    _write_metadata(data_root, str(runtime), str(officecli))
    log = tmp_path / "calls.jsonl"
    recorder = (
        "import json,os,sys;"
        "open(os.environ['CSAF_TEST_LOG'],'a',encoding='utf-8').write(json.dumps({"
        "'args':sys.argv[1:],'office':os.environ.get('CSAF_OFFICECLI'),"
        "'skip':os.environ.get('OFFICECLI_SKIP_UPDATE'),"
        "'pythonpath':os.environ.get('PYTHONPATH')})+'\\n')"
    )
    (tmp_path / "setup").write_text(
        recorder + ";print('Update available. token=must-not-leak')", encoding="utf-8"
    )
    (tmp_path / "account-brief").write_text(
        recorder + ';print(\'{"result":"ok"}\')', encoding="utf-8"
    )
    env = os.environ.copy()
    env.update(
        {
            "CSAF_DATA_ROOT": str(data_root) + os.sep,
            "CSAF_TEST_LOG": str(log),
            "PYTHONPATH": str(tmp_path / "mutable-bootstrap"),
        }
    )

    completed = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(SCRIPTS / "csaf.ps1"),
            "account-brief",
            "acme",
            "--days",
            "90",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == '{"result":"ok"}'
    assert completed.stderr.strip() == (
        "CSAF update available. Run csaf setup update after explicit consent."
    )
    assert "must-not-leak" not in completed.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls == [
        {
            "args": ["check-update"],
            "office": str(officecli),
            "skip": "1",
            "pythonpath": str(runtime / "site-packages"),
        },
        {
            "args": ["acme", "--days", "90"],
            "office": str(officecli),
            "skip": "1",
            "pythonpath": str(runtime / "site-packages"),
        },
    ]


def test_posix_launcher_preserves_argv_and_json_stdout(tmp_path: Path) -> None:
    shell, posix = _posix_tools()
    data_root = tmp_path / "data"
    runtime = data_root / "versions" / VERSION
    runtime.mkdir(parents=True)
    officecli = data_root / "officecli" / OFFICE_VERSION / "officecli"
    officecli.parent.mkdir(parents=True)
    officecli.write_bytes(b"fixture")
    launcher = runtime / "csaf"
    launcher.write_text(
        "#!/bin/sh\n"
        'printf \'%s|%s|%s|%s\\n\' "$*" "$CSAF_OFFICECLI" "$OFFICECLI_SKIP_UPDATE" "$PYTHONPATH" '
        '>> "$CSAF_TEST_LOG"\n'
        'if [ "$1" = setup ]; then\n'
        "  printf '%s\\n' 'Update available. token=must-not-leak'\n"
        "else\n"
        "  printf '%s\\n' '{\"result\":\"ok\"}'\n"
        "fi\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    data_root_text = posix(data_root)
    officecli_text = f"{data_root_text}/officecli/{OFFICE_VERSION}/officecli"
    _write_metadata(
        data_root,
        f"{data_root_text}/versions/{VERSION}",
        officecli_text,
    )
    log = tmp_path / "calls.log"
    env = os.environ.copy()
    env.update(
        {
            "CSAF_DATA_ROOT": data_root_text + "/",
            "CSAF_TEST_LOG": posix(log),
            "PYTHONPATH": posix(tmp_path / "mutable-bootstrap"),
        }
    )

    completed = subprocess.run(
        [
            shell,
            posix(SCRIPTS / "csaf.sh"),
            "qbr",
            "generate",
            "acme",
            "--quarter",
            "2026-Q3",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == '{"result":"ok"}'
    assert completed.stderr.strip() == (
        "CSAF update available. Run csaf setup update after explicit consent."
    )
    assert "must-not-leak" not in completed.stderr
    runtime_site_packages = f"{data_root_text}/versions/{VERSION}/site-packages"
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"setup check-update|{officecli_text}|1|{runtime_site_packages}",
        f"qbr generate acme --quarter 2026-Q3|{officecli_text}|1|{runtime_site_packages}",
    ]


@pytest.mark.parametrize(
    "target,mutation",
    [
        ("current", "extra"),
        ("current", "duplicate"),
        ("current", "trailing"),
        ("current", "boolean-schema"),
        ("current", "bom"),
        ("current", "invalid-utf8"),
        ("state", "extra"),
        ("state", "wrong-type"),
    ],
)
@pytest.mark.parametrize("platform", ["windows", "posix"])
def test_launchers_strictly_reject_malformed_metadata_before_execution(
    tmp_path: Path, platform: str, target: str, mutation: str
) -> None:
    data_root = tmp_path / "data"
    runtime = data_root / "versions" / VERSION
    runtime.mkdir(parents=True)
    office_suffix = "officecli.exe" if platform == "windows" else "officecli"
    officecli = data_root / "officecli" / OFFICE_VERSION / office_suffix
    officecli.parent.mkdir(parents=True)
    officecli.write_bytes(b"fixture")
    marker = tmp_path / "executed"
    if platform == "windows":
        shutil.copy2(sys._base_executable, runtime / "csaf.exe")
        runtime_text = str(runtime)
        office_text = str(officecli)
        command = [
            _powershell(),
            "-NoProfile",
            "-File",
            str(SCRIPTS / "csaf.ps1"),
            str(marker),
        ]
        data_root_env = str(data_root)
    else:
        shell, posix = _posix_tools()
        launcher = runtime / "csaf"
        launcher.write_text(f"#!/bin/sh\ntouch '{posix(marker)}'\n", encoding="utf-8")
        launcher.chmod(0o700)
        runtime_text = f"{posix(data_root)}/versions/{VERSION}"
        office_text = f"{posix(data_root)}/officecli/{OFFICE_VERSION}/officecli"
        command = [shell, posix(SCRIPTS / "csaf.sh"), "doctor"]
        data_root_env = posix(data_root)
    _write_metadata(data_root, runtime_text, office_text)
    path = data_root / f"{target}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "extra":
        payload["unexpected"] = "rejected"
    elif mutation == "boolean-schema":
        payload["schema_version"] = True
    elif mutation == "wrong-type":
        payload["installed_versions"] = VERSION
    text = json.dumps(payload)
    if mutation == "duplicate":
        text = text.replace('"active_version":', '"active_version":"9.9.9","active_version":', 1)
    elif mutation == "trailing":
        text += " trailing"
    if mutation == "bom":
        path.write_text(text, encoding="utf-8-sig")
    elif mutation == "invalid-utf8":
        path.write_bytes(text.encode("utf-8") + b"\xff")
    else:
        path.write_text(text, encoding="utf-8")
    env = {**os.environ, "CSAF_DATA_ROOT": data_root_env}

    completed = subprocess.run(
        command,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    _assert_bootstrap_request(completed, platform)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
@pytest.mark.parametrize("linked_component", ["versions", "officecli"])
def test_powershell_rejects_controlled_junction_before_outside_execution(
    tmp_path: Path, linked_component: str
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / linked_component
    target.mkdir()
    junction = str(data_root / linked_component).replace("'", "''")
    target_text = str(target).replace("'", "''")
    subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-Command",
            f"New-Item -ItemType Junction -Path '{junction}' -Target '{target_text}' | Out-Null",
        ],
        check=True,
    )
    runtime = data_root / "versions" / VERSION
    runtime.mkdir(parents=True)
    marker = tmp_path / "outside-executed"
    (tmp_path / "setup").write_text(
        f"from pathlib import Path; Path(r''{marker}'').write_text(''called'')",
        encoding="utf-8",
    )
    shutil.copy2(sys._base_executable, runtime / "csaf.exe")
    officecli = data_root / "officecli" / OFFICE_VERSION / "officecli.exe"
    officecli.parent.mkdir(parents=True)
    officecli.write_bytes(b"fixture")
    _write_metadata(data_root, str(runtime), str(officecli))

    completed = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(SCRIPTS / "csaf.ps1"),
            str(marker),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "CSAF_DATA_ROOT": str(data_root)},
        cwd=tmp_path,
        check=False,
    )

    _assert_bootstrap_request(completed, "windows")
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink contract")
@pytest.mark.parametrize("linked_component", ["versions", "officecli"])
def test_posix_rejects_controlled_symlink_before_outside_execution(
    tmp_path: Path, linked_component: str
) -> None:
    shell, posix = _posix_tools()
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / linked_component
    target.mkdir()
    (data_root / linked_component).symlink_to(target, target_is_directory=True)
    runtime = data_root / "versions" / VERSION
    runtime.mkdir(parents=True)
    marker = tmp_path / "outside-executed"
    launcher = runtime / "csaf"
    launcher.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    launcher.chmod(0o700)
    officecli = data_root / "officecli" / OFFICE_VERSION / "officecli"
    officecli.parent.mkdir(parents=True)
    officecli.write_bytes(b"fixture")
    _write_metadata(data_root, str(runtime), str(officecli))

    completed = subprocess.run(
        [shell, posix(SCRIPTS / "csaf.sh"), "doctor"],
        text=True,
        capture_output=True,
        env={**os.environ, "CSAF_DATA_ROOT": str(data_root)},
        check=False,
    )

    _assert_bootstrap_request(completed, "posix")
    assert not marker.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "installed-version-boolean",
        "installed-version-syntax",
        "runtime-key-syntax",
        "runtime-relative-value",
        "runtime-outside-value",
        "checksum-key-schema",
        "checksum-value-schema",
        "checksum-version-consistency",
        "adapter-key-enum",
        "adapter-relative-value",
    ],
)
@pytest.mark.parametrize("platform", ["windows", "posix"])
def test_launchers_reject_invalid_nested_install_state_before_execution(
    tmp_path: Path, platform: str, mutation: str
) -> None:
    if platform == "windows" and os.name != "nt":
        pytest.skip("Windows launcher contract requires a Windows host")
    data_root = tmp_path / "data"
    runtime = data_root / "versions" / VERSION
    runtime.mkdir(parents=True)
    office_suffix = "officecli.exe" if platform == "windows" else "officecli"
    officecli = data_root / "officecli" / OFFICE_VERSION / office_suffix
    officecli.parent.mkdir(parents=True)
    officecli.write_bytes(b"fixture")
    marker = tmp_path / "executed"
    if platform == "windows":
        command_exe = Path(os.environ.get("COMSPEC", "C:/Windows/System32/cmd.exe"))
        shutil.copy2(command_exe, runtime / "csaf.exe")
        runtime_text = str(runtime)
        office_text = str(officecli)
        command = [
            _powershell(),
            "-NoProfile",
            "-File",
            str(SCRIPTS / "csaf.ps1"),
            "/d",
            "/c",
            f'type nul > "{marker}"',
        ]
        data_root_env = str(data_root)
        extra_runtime = str(data_root / "versions" / "0.2.0")
        outside_runtime = str(tmp_path / "outside-runtime")
        adapter_path = str(tmp_path / "adapter")
    else:
        shell, posix = _posix_tools()
        launcher = runtime / "csaf"
        launcher.write_text(f"#!/bin/sh\ntouch '{posix(marker)}'\n", encoding="utf-8")
        launcher.chmod(0o700)
        runtime_text = f"{posix(data_root)}/versions/{VERSION}"
        office_text = f"{posix(data_root)}/officecli/{OFFICE_VERSION}/officecli"
        command = [shell, posix(SCRIPTS / "csaf.sh"), "doctor"]
        data_root_env = posix(data_root)
        extra_runtime = f"{posix(data_root)}/versions/0.2.0"
        outside_runtime = posix(tmp_path / "outside-runtime")
        adapter_path = posix(tmp_path / "adapter")
    _write_metadata(data_root, runtime_text, office_text)
    state_path = data_root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if mutation == "installed-version-boolean":
        state["installed_versions"] = [VERSION, True]
    elif mutation == "installed-version-syntax":
        state["installed_versions"] = [VERSION, "v1"]
    elif mutation == "runtime-key-syntax":
        state["runtime_paths"]["v1"] = extra_runtime
    elif mutation == "runtime-relative-value":
        state["installed_versions"].append("0.2.0")
        state["runtime_paths"]["0.2.0"] = "relative/runtime"
    elif mutation == "runtime-outside-value":
        state["installed_versions"].append("0.2.0")
        state["runtime_paths"]["0.2.0"] = outside_runtime
    elif mutation == "checksum-key-schema":
        state["verified_checksums"]["unexpected"] = "b" * 64
    elif mutation == "checksum-value-schema":
        state["verified_checksums"][f"runtime:{VERSION}"] = "B" * 64
    elif mutation == "checksum-version-consistency":
        state["verified_checksums"]["runtime:9.9.9"] = "b" * 64
    elif mutation == "adapter-key-enum":
        state["adapter_targets"]["gemini"] = adapter_path
    elif mutation == "adapter-relative-value":
        state["adapter_targets"]["codex"] = "relative/adapter"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    completed = subprocess.run(
        command,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "CSAF_DATA_ROOT": data_root_env},
        check=False,
    )

    _assert_bootstrap_request(completed, platform)
    assert not marker.exists()


@pytest.mark.parametrize("platform", ["windows", "posix"])
def test_launchers_accept_valid_nonempty_nested_install_state(
    tmp_path: Path, platform: str
) -> None:
    if platform == "windows" and os.name != "nt":
        pytest.skip("Windows launcher contract requires a Windows host")
    data_root = tmp_path / "data"
    runtime = data_root / "versions" / VERSION
    extra_runtime = data_root / "versions" / "0.2.0"
    runtime.mkdir(parents=True)
    extra_runtime.mkdir(parents=True)
    office_suffix = "officecli.exe" if platform == "windows" else "officecli"
    officecli = data_root / "officecli" / OFFICE_VERSION / office_suffix
    officecli.parent.mkdir(parents=True)
    officecli.write_bytes(b"fixture")
    marker = tmp_path / "executed"
    if platform == "windows":
        shutil.copy2(
            Path(os.environ.get("COMSPEC", "C:/Windows/System32/cmd.exe")),
            runtime / "csaf.exe",
        )
        runtime_text = str(runtime)
        extra_runtime_text = str(extra_runtime)
        office_text = str(officecli)
        adapter_text = str(tmp_path / "adapter")
        command = [
            _powershell(),
            "-NoProfile",
            "-File",
            str(SCRIPTS / "csaf.ps1"),
            "/d",
            "/c",
            f'type nul > "{marker}"',
        ]
        data_root_env = str(data_root)
    else:
        shell, posix = _posix_tools()
        launcher = runtime / "csaf"
        launcher.write_text(f"#!/bin/sh\ntouch '{posix(marker)}'\n", encoding="utf-8")
        launcher.chmod(0o700)
        runtime_text = f"{posix(data_root)}/versions/{VERSION}"
        extra_runtime_text = f"{posix(data_root)}/versions/0.2.0"
        office_text = f"{posix(data_root)}/officecli/{OFFICE_VERSION}/officecli"
        adapter_text = posix(tmp_path / "adapter")
        command = [shell, posix(SCRIPTS / "csaf.sh"), "doctor"]
        data_root_env = posix(data_root)
    _write_metadata(data_root, runtime_text, office_text)
    state_path = data_root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["installed_versions"] = [VERSION, "0.2.0"]
    state["runtime_paths"] = {VERSION: runtime_text, "0.2.0": extra_runtime_text}
    state["verified_checksums"] = {
        f"runtime:{VERSION}": "b" * 64,
        "runtime-content:0.2.0": "c" * 64,
        f"officecli:{OFFICE_VERSION}": "d" * 64,
        f"adapter:codex:{VERSION}": "e" * 64,
    }
    state["adapter_targets"] = {"codex": adapter_text}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    completed = subprocess.run(
        command,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "CSAF_DATA_ROOT": data_root_env},
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_powershell_rejects_junction_data_root_before_outside_execution(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-root"
    outside.mkdir()
    data_root = tmp_path / "data"
    junction = str(data_root).replace("'", "''")
    target_text = str(outside).replace("'", "''")
    subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-Command",
            f"New-Item -ItemType Junction -Path '{junction}' -Target '{target_text}' | Out-Null",
        ],
        check=True,
    )
    runtime = data_root / "versions" / VERSION
    runtime.mkdir(parents=True)
    shutil.copy2(sys._base_executable, runtime / "csaf.exe")
    officecli = data_root / "officecli" / OFFICE_VERSION / "officecli.exe"
    officecli.parent.mkdir(parents=True)
    officecli.write_bytes(b"fixture")
    _write_metadata(data_root, str(runtime), str(officecli))
    marker = tmp_path / "outside-executed"
    (tmp_path / "setup").write_text(
        f"from pathlib import Path; Path(r'{marker}').write_text('called')",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-File", str(SCRIPTS / "csaf.ps1"), "doctor"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "CSAF_DATA_ROOT": str(data_root)},
        check=False,
    )

    _assert_bootstrap_request(completed, "windows")
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink contract")
def test_posix_rejects_symlink_data_root_before_outside_execution(tmp_path: Path) -> None:
    shell, posix = _posix_tools()
    outside = tmp_path / "outside-root"
    outside.mkdir()
    data_root = tmp_path / "data"
    data_root.symlink_to(outside, target_is_directory=True)
    runtime = data_root / "versions" / VERSION
    runtime.mkdir(parents=True)
    marker = tmp_path / "outside-executed"
    launcher = runtime / "csaf"
    launcher.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    launcher.chmod(0o700)
    officecli = data_root / "officecli" / OFFICE_VERSION / "officecli"
    officecli.parent.mkdir(parents=True)
    officecli.write_bytes(b"fixture")
    _write_metadata(data_root, str(runtime), str(officecli))

    completed = subprocess.run(
        [shell, posix(SCRIPTS / "csaf.sh"), "doctor"],
        text=True,
        capture_output=True,
        env={**os.environ, "CSAF_DATA_ROOT": str(data_root)},
        check=False,
    )

    _assert_bootstrap_request(completed, "posix")
    assert not marker.exists()


def test_powershell_rejects_relative_data_root_before_execution(tmp_path: Path) -> None:
    relative = tmp_path / "relative"
    runtime = relative / "versions" / VERSION
    runtime.mkdir(parents=True)
    shutil.copy2(sys._base_executable, runtime / "csaf.exe")
    officecli = relative / "officecli" / OFFICE_VERSION / "officecli.exe"
    officecli.parent.mkdir(parents=True)
    officecli.write_bytes(b"fixture")
    _write_metadata(relative, str(runtime), str(officecli))
    marker = tmp_path / "executed"

    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-File", str(SCRIPTS / "csaf.ps1"), str(marker)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "CSAF_DATA_ROOT": "relative"},
        check=False,
    )

    _assert_bootstrap_request(completed, "windows")
    assert not marker.exists()


def test_posix_rejects_relative_xdg_and_home_roots_without_writes(tmp_path: Path) -> None:
    shell, posix = _posix_tools()
    for variable in ("XDG_DATA_HOME", "HOME"):
        env = os.environ.copy()
        env.pop("CSAF_DATA_ROOT", None)
        env["HOME"] = posix(tmp_path / "home")
        env.pop("XDG_DATA_HOME", None)
        env[variable] = "relative"
        completed = subprocess.run(
            [shell, posix(SCRIPTS / "csaf.sh"), "doctor"],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        _assert_bootstrap_request(completed, "posix")
        assert not (tmp_path / "relative").exists()


def test_launcher_sources_use_strict_parsers_and_no_shell_interpolation() -> None:
    ps = (SCRIPTS / "csaf.ps1").read_text(encoding="utf-8")
    sh = (SCRIPTS / "csaf.sh").read_text(encoding="utf-8")

    assert "Invoke-Expression" not in ps
    assert "Start-Process" not in ps
    assert "Read-StrictJson" in ps
    assert "ConvertFrom-Json" in ps
    assert "OFFICECLI_SKIP_UPDATE" in ps
    assert "eval " not in sh
    assert "grep " not in sh
    assert "sed " not in sh
    assert "parse_value" in sh
    assert "exact_object" in sh
    assert "OFFICECLI_SKIP_UPDATE" in sh
    assert '"$@"' in sh
