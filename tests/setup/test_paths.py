from pathlib import Path

import pytest

from csaf.setup import AssistantKind, SupportedPlatform
from csaf.setup.paths import (
    codex_skill_root,
    current_platform,
    default_data_root,
    detect_assistants,
)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Windows", "AMD64", SupportedPlatform.WINDOWS_X64),
        ("Windows", "X64", SupportedPlatform.WINDOWS_X64),
        ("windows", "x86_64", SupportedPlatform.WINDOWS_X64),
        ("Windows", "arm64", SupportedPlatform.WINDOWS_ARM64),
        ("Darwin", "x86_64", SupportedPlatform.MACOS_X64),
        ("macOS", "aarch64", SupportedPlatform.MACOS_ARM64),
        ("Linux", "AMD64", SupportedPlatform.LINUX_X64),
        ("Linux", "x64", SupportedPlatform.LINUX_X64),
        ("linux", "arm64", SupportedPlatform.LINUX_ARM64),
    ],
)
def test_current_platform_normalizes_common_aliases(
    system: str,
    machine: str,
    expected: SupportedPlatform,
) -> None:
    assert current_platform(system, machine) is expected


@pytest.mark.parametrize(
    ("system", "machine", "message"),
    [
        ("Solaris", "x86_64", "unsupported operating system"),
        ("Linux", "riscv64", "unsupported architecture"),
    ],
)
def test_current_platform_rejects_unsupported_values(
    system: str,
    machine: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        current_platform(system, machine)


def test_default_data_roots_are_platform_native(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    xdg = tmp_path / "xdg"

    assert (
        default_data_root(system="Windows", home=tmp_path, environ={"LOCALAPPDATA": str(local)})
        == local / "CSAF"
    )
    assert (
        default_data_root(system="Darwin", home=tmp_path, environ={})
        == tmp_path / "Library" / "Application Support" / "CSAF"
    )
    assert (
        default_data_root(system="Linux", home=tmp_path, environ={"XDG_DATA_HOME": str(xdg)})
        == xdg / "csaf"
    )
    assert (
        default_data_root(system="Linux", home=tmp_path, environ={})
        == tmp_path / ".local" / "share" / "csaf"
    )


def test_windows_data_root_has_safe_home_fallback(tmp_path: Path) -> None:
    assert (
        default_data_root(system="Windows", home=tmp_path, environ={})
        == tmp_path / "AppData" / "Local" / "CSAF"
    )


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_explicit_csaf_data_root_overrides_platform_default(tmp_path: Path, system: str) -> None:
    configured = tmp_path / "configured-csaf"

    assert (
        default_data_root(
            system=system,
            home=tmp_path / "home",
            environ={"CSAF_DATA_ROOT": str(configured)},
        )
        == configured
    )


@pytest.mark.parametrize("configured", ["relative/csaf", ".", "../csaf"])
def test_explicit_csaf_data_root_must_be_absolute(tmp_path: Path, configured: str) -> None:
    with pytest.raises(ValueError, match="CSAF_DATA_ROOT must be absolute"):
        default_data_root(
            system="Linux",
            home=tmp_path,
            environ={"CSAF_DATA_ROOT": configured},
        )


def test_explicit_csaf_data_root_rejects_filesystem_root(tmp_path: Path) -> None:
    filesystem_root = Path(tmp_path.anchor)

    with pytest.raises(ValueError, match="must not be a filesystem root"):
        default_data_root(
            system="Windows" if filesystem_root.drive else "Linux",
            home=tmp_path,
            environ={"CSAF_DATA_ROOT": str(filesystem_root)},
        )


def test_codex_skill_root_honors_codex_home_and_falls_back(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    assert (
        codex_skill_root(home=tmp_path, environ={"CODEX_HOME": str(configured)})
        == configured / "skills"
    )
    assert codex_skill_root(home=tmp_path, environ={}) == tmp_path / ".codex" / "skills"


@pytest.mark.parametrize(
    ("codex_home", "claude_home", "executables", "expected"),
    [
        (True, True, set(), (AssistantKind.CODEX, AssistantKind.CLAUDE)),
        (False, False, {"codex", "claude"}, (AssistantKind.CODEX, AssistantKind.CLAUDE)),
        (True, False, set(), (AssistantKind.CODEX,)),
        (False, True, set(), (AssistantKind.CLAUDE,)),
        (False, False, set(), ()),
    ],
)
def test_detect_assistants_uses_injected_roots_and_executables(
    tmp_path: Path,
    codex_home: bool,
    claude_home: bool,
    executables: set[str],
    expected: tuple[AssistantKind, ...],
) -> None:
    home = tmp_path / "injected-home"
    home.mkdir()
    environ: dict[str, str] = {}
    if codex_home:
        configured = tmp_path / "configured-codex"
        (configured / "skills").mkdir(parents=True)
        environ["CODEX_HOME"] = str(configured)
    if claude_home:
        (home / ".claude").mkdir()

    detected = detect_assistants(
        home=home,
        environ=environ,
        which=lambda name: f"/bin/{name}" if name in executables else None,
    )

    assert detected == expected


def test_detect_assistants_does_not_use_developer_home(tmp_path: Path) -> None:
    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    assert detect_assistants(home=empty_home, environ={}, which=lambda _: None) == ()


def test_explicit_codex_home_detects_codex_before_skills_exist(tmp_path: Path) -> None:
    detected = detect_assistants(
        home=tmp_path,
        environ={"CODEX_HOME": str(tmp_path / "codex")},
        which=lambda name: f"/bin/{name}" if name == "claude" else None,
    )
    assert detected == (AssistantKind.CODEX, AssistantKind.CLAUDE)


def test_relative_xdg_data_home_uses_safe_home_fallback(tmp_path: Path) -> None:
    assert (
        default_data_root(
            system="Linux",
            home=tmp_path,
            environ={"XDG_DATA_HOME": "relative/data"},
        )
        == tmp_path / ".local" / "share" / "csaf"
    )


def test_setup_exports_all_public_helpers() -> None:
    import csaf.setup as setup

    expected = (
        current_platform,
        default_data_root,
        codex_skill_root,
        detect_assistants,
    )
    assert (
        setup.current_platform,
        setup.default_data_root,
        setup.codex_skill_root,
        setup.detect_assistants,
    ) == expected
    for name in (
        "download_verified",
        "extract_verified_archive",
        "write_json_atomic",
        "read_json",
    ):
        assert callable(getattr(setup, name))
