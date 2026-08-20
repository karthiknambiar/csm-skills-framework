from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

import pytest

from csaf.setup import AssistantKind, SetupError, Version
from csaf.setup.adapters import (
    AdapterInstallResult,
    ClaudeAdapterInstaller,
    CodexAdapterInstaller,
    _safe_detail,
    _subprocess_runner,
    install_adapters,
)


class RecordingRunner:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        failure: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.failure = failure
        self.responses: list[tuple[int, bytes, bytes] | Exception] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(command), timeout))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            returncode, response_stdout, response_stderr = response
            stdout.write(response_stdout)
            stderr.write(response_stderr)
            return subprocess.CompletedProcess(list(command), returncode)
        if self.failure is not None:
            raise self.failure
        stdout.write(self.stdout)
        stderr.write(self.stderr)
        return subprocess.CompletedProcess(list(command), self.returncode)


class RecordingInstaller:
    def __init__(self, kind: AssistantKind, target: Path | None = None) -> None:
        self.kind = kind
        self.target = target
        self.calls = 0

    def install(self) -> AdapterInstallResult:
        self.calls += 1
        return AdapterInstallResult(self.kind, self.target)


def _skill_source(root: Path, *, marker: str = "new") -> Path:
    source = root / "source"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(marker, encoding="utf-8")
    scripts = source / "scripts"
    scripts.mkdir()
    (scripts / "csaf.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return source


def test_codex_installs_skill_and_records_exact_target(tmp_path: Path) -> None:
    source = _skill_source(tmp_path)
    skill_root = tmp_path / "codex" / "skills"

    result = CodexAdapterInstaller(source, skill_root).install()

    target = skill_root / "csaf"
    assert result == AdapterInstallResult(AssistantKind.CODEX, target)
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "new"
    assert (target / "scripts" / "csaf.sh").is_file()
    assert not (skill_root / ".csaf.staging").exists()
    assert not (skill_root / ".csaf.backup").exists()


def test_codex_root_creation_failure_is_stable_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _skill_source(tmp_path)
    skill_root = tmp_path / "private" / "codex" / "skills"
    original = Path.mkdir
    secret = "sk-" + ("A" * 32)

    def fail_root(path: Path, *args: object, **kwargs: object) -> None:
        if path == skill_root:
            raise OSError(f"SECRET {secret} /Users/Alice/private/skills")
        original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_root)

    with pytest.raises(SetupError) as caught:
        CodexAdapterInstaller(source, skill_root).install()

    assert str(caught.value) == "Codex adapter installation failed"
    assert secret not in str(caught.value)
    assert "Alice" not in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)
    assert secret in str(caught.value.__cause__)


def test_codex_keeps_working_adapter_when_replacement_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _skill_source(tmp_path)
    skill_root = tmp_path / "codex" / "skills"
    target = skill_root / "csaf"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old", encoding="utf-8")

    def fail_copy(source_path: Path, staging_path: Path, **kwargs: object) -> Path:
        del source_path, staging_path, kwargs
        raise OSError("SECRET copy path")

    monkeypatch.setattr("csaf.setup.adapters._copy_skill_tree", fail_copy)

    with pytest.raises(SetupError, match="Codex adapter installation failed") as caught:
        CodexAdapterInstaller(source, skill_root).install()

    assert "SECRET" not in str(caught.value)
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "old"


def test_codex_restores_existing_adapter_when_activation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _skill_source(tmp_path)
    skill_root = tmp_path / "codex" / "skills"
    target = skill_root / "csaf"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old", encoding="utf-8")
    real_replace = __import__("os").replace
    replacements = 0

    def fail_new_activation(source_path: Path, destination_path: Path) -> None:
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("activation failed")
        real_replace(source_path, destination_path)

    monkeypatch.setattr("csaf.setup.adapters.os.replace", fail_new_activation)

    with pytest.raises(SetupError, match="Codex adapter installation failed"):
        CodexAdapterInstaller(source, skill_root).install()

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "old"


def test_claude_uses_exact_argument_arrays_and_timeout() -> None:
    runner = RecordingRunner()
    runner.responses = [(0, b"[]", b""), (0, b"[]", b""), (0, b"", b""), (0, b"", b"")]

    result = ClaudeAdapterInstaller(Version("0.1.0"), runner=runner, timeout=17).install()

    assert result == AdapterInstallResult(AssistantKind.CLAUDE, None)
    assert runner.calls[-2:] == [
        (
            [
                "claude",
                "plugin",
                "marketplace",
                "add",
                "https://github.com/karthiknambiar/csm-skills-framework.git#v0.1.0",
            ],
            17,
        ),
        (["claude", "plugin", "install", "csaf@csaf", "--scope", "user"], 17),
    ]


@pytest.mark.parametrize(
    ("detected", "codex_only", "claude_only", "expected"),
    [
        ((AssistantKind.CODEX,), False, False, (AssistantKind.CODEX,)),
        ((AssistantKind.CLAUDE,), False, False, (AssistantKind.CLAUDE,)),
        (
            (AssistantKind.CODEX, AssistantKind.CLAUDE),
            False,
            False,
            (AssistantKind.CODEX, AssistantKind.CLAUDE),
        ),
        ((), False, False, ()),
        (
            (AssistantKind.CODEX, AssistantKind.CLAUDE),
            True,
            False,
            (AssistantKind.CODEX,),
        ),
        (
            (AssistantKind.CODEX, AssistantKind.CLAUDE),
            False,
            True,
            (AssistantKind.CLAUDE,),
        ),
    ],
)
def test_installs_selected_detected_adapters(
    tmp_path: Path,
    detected: tuple[AssistantKind, ...],
    codex_only: bool,
    claude_only: bool,
    expected: tuple[AssistantKind, ...],
) -> None:
    codex = RecordingInstaller(AssistantKind.CODEX, tmp_path / "codex")
    claude = RecordingInstaller(AssistantKind.CLAUDE)

    results = install_adapters(
        detected,
        {AssistantKind.CODEX: codex, AssistantKind.CLAUDE: claude},
        codex_only=codex_only,
        claude_only=claude_only,
    )

    assert tuple(result.kind for result in results) == expected
    assert codex.calls == (1 if AssistantKind.CODEX in expected else 0)
    assert claude.calls == (1 if AssistantKind.CLAUDE in expected else 0)


def test_adapter_selection_rejects_conflicting_overrides() -> None:
    with pytest.raises(SetupError, match="cannot be used together"):
        install_adapters((), {}, codex_only=True, claude_only=True)


def test_adapter_selection_rejects_undetected_explicit_target() -> None:
    with pytest.raises(SetupError, match="requested assistant was not detected"):
        install_adapters((), {}, codex_only=True)


def test_adapter_selection_rejects_missing_installer() -> None:
    with pytest.raises(SetupError, match="installer is unavailable"):
        install_adapters((AssistantKind.CODEX,), {})


def test_claude_command_failure_is_sanitized() -> None:
    secret = "sk-" + ("A" * 32)
    runner = RecordingRunner()
    runner.responses = [
        (0, b"[]", b""),
        (0, b"[]", b""),
        (7, b"", f"failed token={secret}".encode()),
    ]

    with pytest.raises(SetupError, match="Claude Code marketplace add failed") as caught:
        ClaudeAdapterInstaller(Version("0.1.0"), runner=runner).install()

    assert secret not in str(caught.value)
    assert "<redacted-secret>" in str(caught.value)
    assert len(runner.calls) == 3
    assert runner.calls[-1][0][2:4] == ["marketplace", "add"]


def test_claude_timeout_is_stable_and_does_not_run_second_command() -> None:
    runner = RecordingRunner(failure=subprocess.TimeoutExpired(["claude", "plugin"], timeout=3))

    with pytest.raises(SetupError, match="exceeded the 3s timeout"):
        ClaudeAdapterInstaller(Version("0.1.0"), runner=runner, timeout=3).install()

    assert len(runner.calls) == 1


def test_claude_invalid_utf8_is_rejected_without_echoing_bytes() -> None:
    runner = RecordingRunner(stderr=b"invalid-\xff-secret")

    with pytest.raises(SetupError, match="not valid UTF-8") as caught:
        ClaudeAdapterInstaller(Version("0.1.0"), runner=runner).install()

    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_claude_rejects_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        ClaudeAdapterInstaller(Version("0.1.0"), timeout=timeout)


def test_codex_rejects_source_symlink_without_copying(tmp_path: Path) -> None:
    source = _skill_source(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = source / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(SetupError, match="link or reparse point"):
        CodexAdapterInstaller(source, tmp_path / "skills").install()

    assert not (tmp_path / "skills" / "csaf").exists()


def test_codex_rejects_mocked_windows_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _skill_source(tmp_path)
    scripts = source / "scripts"
    original = Path.lstat

    def junction_lstat(path: Path):
        details = original(path)
        if path == scripts:

            class JunctionStat:
                st_mode = details.st_mode
                st_file_attributes = 0x400
                st_dev = details.st_dev
                st_ino = details.st_ino

            return JunctionStat()
        return details

    monkeypatch.setattr(Path, "lstat", junction_lstat)

    with pytest.raises(SetupError, match="link or reparse point"):
        CodexAdapterInstaller(source, tmp_path / "skills").install()


def test_codex_rejects_mocked_symlink_on_every_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _skill_source(tmp_path)
    scripts = source / "scripts"
    original = Path.lstat

    def symlink_lstat(path: Path):
        details = original(path)
        if path == scripts:

            class LinkStat:
                st_mode = stat.S_IFLNK | 0o777
                st_file_attributes = 0
                st_dev = details.st_dev
                st_ino = details.st_ino

            return LinkStat()
        return details

    monkeypatch.setattr(Path, "lstat", symlink_lstat)

    with pytest.raises(SetupError, match="link or reparse point"):
        CodexAdapterInstaller(source, tmp_path / "skills").install()


def _capture_install_error(installer: CodexAdapterInstaller, errors: list[Exception]) -> None:
    try:
        installer.install()
    except Exception as error:  # pragma: no cover - assertion reports worker failures
        errors.append(error)


def test_codex_serializes_concurrent_installers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _skill_source(tmp_path)
    skill_root = tmp_path / "skills"
    entered = threading.Event()
    release = threading.Event()
    module = __import__("csaf.setup.adapters", fromlist=["_copy_skill_tree"])
    original = module._copy_skill_tree

    def blocked_copy(source_path: Path, destination_path: Path) -> None:
        entered.set()
        assert release.wait(timeout=5)
        original(source_path, destination_path)

    monkeypatch.setattr("csaf.setup.adapters._copy_skill_tree", blocked_copy)
    first_errors: list[Exception] = []
    thread = threading.Thread(
        target=lambda: _capture_install_error(
            CodexAdapterInstaller(source, skill_root), first_errors
        )
    )
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(SetupError, match="already in progress"):
            CodexAdapterInstaller(source, skill_root).install()
    finally:
        release.set()
        thread.join(timeout=5)
    assert not first_errors


def test_codex_recovers_stale_backup_before_failed_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _skill_source(tmp_path)
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    backup = skill_root / ".csaf.backup"
    backup.mkdir()
    (backup / "SKILL.md").write_text("old", encoding="utf-8")
    stale = skill_root / ".csaf.staging"
    stale.mkdir()

    def fail_copy(source_path: Path, destination_path: Path) -> None:
        del source_path, destination_path
        raise OSError("copy failed")

    monkeypatch.setattr("csaf.setup.adapters._copy_skill_tree", fail_copy)

    with pytest.raises(SetupError, match="Codex adapter installation failed"):
        CodexAdapterInstaller(source, skill_root).install()

    assert (skill_root / "csaf" / "SKILL.md").read_text(encoding="utf-8") == "old"
    assert not stale.exists()


def test_codex_cleanup_failure_reports_activated_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _skill_source(tmp_path)
    skill_root = tmp_path / "skills"
    target = skill_root / "csaf"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old", encoding="utf-8")
    module = __import__("csaf.setup.adapters", fromlist=["_remove_tree"])
    original = module._remove_tree

    def fail_backup_cleanup(path: Path) -> None:
        if path.name == ".csaf.backup":
            raise OSError("SECRET cleanup path")
        original(path)

    monkeypatch.setattr("csaf.setup.adapters._remove_tree", fail_backup_cleanup)

    with pytest.raises(SetupError, match="cleanup is incomplete") as caught:
        CodexAdapterInstaller(source, skill_root).install()

    assert caught.value.activated is True
    assert "SECRET" not in str(caught.value)


def test_claude_repeat_install_skips_existing_marketplace_and_plugin() -> None:
    runner = RecordingRunner()
    marketplace = (
        b'[{"name":"csaf","url":"https://github.com/karthiknambiar/'
        b'csm-skills-framework.git","ref":"v0.1.0"}]'
    )
    plugin = b'[{"name":"csaf","marketplace":"csaf","scope":"user"}]'
    runner.responses = [(0, marketplace, b""), (0, plugin, b"")]

    result = ClaudeAdapterInstaller(Version("0.1.0"), runner=runner).install()

    assert result == AdapterInstallResult(AssistantKind.CLAUDE, None)
    assert [call[0] for call in runner.calls] == [
        ["claude", "plugin", "marketplace", "list", "--json"],
        ["claude", "plugin", "list", "--json"],
    ]


def test_claude_second_command_failure_rolls_back_new_marketplace() -> None:
    runner = RecordingRunner()
    runner.responses = [
        (0, b"[]", b""),
        (0, b"[]", b""),
        (0, b"", b""),
        (9, b"", b"install failed"),
        (0, b"", b""),
    ]

    with pytest.raises(SetupError, match="plugin install failed") as caught:
        ClaudeAdapterInstaller(Version("0.1.0"), runner=runner).install()

    assert caught.value.activated is False
    assert runner.calls[-1][0] == [
        "claude",
        "plugin",
        "marketplace",
        "remove",
        "csaf",
    ]


def test_claude_rollback_failure_reports_partial_state() -> None:
    runner = RecordingRunner()
    runner.responses = [
        (0, b"[]", b""),
        (0, b"[]", b""),
        (0, b"", b""),
        (9, b"", b"install failed"),
        (8, b"", b"token=secret-value"),
    ]

    with pytest.raises(SetupError, match="marketplace remains installed") as caught:
        ClaudeAdapterInstaller(Version("0.1.0"), runner=runner).install()

    assert caught.value.activated is True
    assert "secret-value" not in str(caught.value)


def test_claude_bounds_command_output_and_sanitizes_controls() -> None:
    runner = RecordingRunner()
    runner.responses = [(7, b"", b"bad\x1b[31m\nsecret=" + b"x" * 100_000)]

    with pytest.raises(SetupError) as caught:
        ClaudeAdapterInstaller(Version("0.1.0"), runner=runner).install()

    message = str(caught.value)
    assert "\x1b" not in message
    assert "\n" not in message
    assert len(message) < 5000


def test_codex_post_activation_fsync_failure_reports_activated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _skill_source(tmp_path)
    skill_root = tmp_path / "skills"
    target = skill_root / "csaf"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old", encoding="utf-8")
    module = __import__("csaf.setup.adapters", fromlist=["_fsync_directory"])
    original = module._fsync_directory

    def fail_after_activation(path: Path) -> None:
        if path == skill_root and (target / "SKILL.md").is_file():
            if (target / "SKILL.md").read_text(encoding="utf-8") == "new":
                raise OSError("SECRET durability path")
        original(path)

    monkeypatch.setattr("csaf.setup.adapters._fsync_directory", fail_after_activation)

    with pytest.raises(SetupError, match="durability is uncertain") as caught:
        CodexAdapterInstaller(source, skill_root).install()

    assert caught.value.activated is True
    assert "SECRET" not in str(caught.value)


def test_claude_malformed_state_fails_before_mutation_or_rollback() -> None:
    runner = RecordingRunner()
    marketplace = (
        b'[{"name":"csaf","url":"https://github.com/karthiknambiar/'
        b'csm-skills-framework.git","ref":"v0.1.0"}]'
    )
    runner.responses = [(0, marketplace, b""), (0, b"{}", b"")]

    with pytest.raises(SetupError, match="plugin list returned an unexpected response"):
        ClaudeAdapterInstaller(Version("0.1.0"), runner=runner).install()

    assert [call[0] for call in runner.calls] == [
        ["claude", "plugin", "marketplace", "list", "--json"],
        ["claude", "plugin", "list", "--json"],
    ]


def _pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _pid_stops_within(pid: int, timeout: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while _pid_is_running(pid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def test_subprocess_timeout_kills_pipe_inheriting_descendant_without_hanging(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_code = "import time; time.sleep(4)"
    parent_code = (
        "import pathlib,subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(p.pid));"
        "time.sleep(4)"
    )
    started = time.monotonic()
    with tempfile.TemporaryFile(mode="w+b") as stdout:
        with tempfile.TemporaryFile(mode="w+b") as stderr:
            with pytest.raises(subprocess.TimeoutExpired):
                _subprocess_runner(
                    [sys.executable, "-c", parent_code],
                    stdout=stdout,
                    stderr=stderr,
                    timeout=0.25,
                )
    elapsed = time.monotonic() - started

    assert elapsed < 2
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(50):
        if not _pid_is_running(child_pid):
            break
        time.sleep(0.02)
    assert not _pid_is_running(child_pid)


@pytest.mark.parametrize(
    "marketplaces",
    [
        [
            {
                "name": "csaf",
                "url": "https://github.com/karthiknambiar/csm-skills-framework.git",
            }
        ],
        [
            {
                "name": "csaf",
                "url": "https://github.com/karthiknambiar/csm-skills-framework.git",
                "ref": "v9.9.9",
            }
        ],
        [
            {
                "name": "unrelated",
                "url": "https://github.com/karthiknambiar/csm-skills-framework.git",
                "ref": "v0.1.0",
            }
        ],
    ],
)
def test_claude_rejects_nonexact_marketplace_pin_before_plugin_actions(
    marketplaces: list[dict[str, str]],
) -> None:
    runner = RecordingRunner()
    runner.responses = [(0, json.dumps(marketplaces).encode(), b"")]

    with pytest.raises(SetupError, match="marketplace"):
        ClaudeAdapterInstaller(Version("0.1.0"), runner=runner).install()

    assert [call[0] for call in runner.calls] == [
        ["claude", "plugin", "marketplace", "list", "--json"]
    ]


def test_claude_accepts_exact_combined_marketplace_source() -> None:
    runner = RecordingRunner()
    source = "https://github.com/karthiknambiar/csm-skills-framework.git#v0.1.0"
    runner.responses = [
        (0, json.dumps([{"name": "csaf", "source": source}]).encode(), b""),
        (0, b'[{"name":"csaf@csaf","scope":"user"}]', b""),
    ]

    result = ClaudeAdapterInstaller(Version("0.1.0"), runner=runner).install()

    assert result == AdapterInstallResult(AssistantKind.CLAUDE, None)
    assert len(runner.calls) == 2


def test_safe_detail_removes_terminal_sequences_controls_and_bidi() -> None:
    escape = chr(27)
    message = (
        "bad "
        + escape
        + "[31mred"
        + escape
        + "[0m "
        + escape
        + "]0;window-title"
        + chr(7)
        + " "
        + escape
        + "Pprivate-data"
        + escape
        + chr(92)
        + " c1"
        + chr(0x9B)
        + "31mcolor "
        + chr(7)
        + "bell "
        + chr(8)
        + "back "
        + chr(0x85)
        + "next bidi"
        + chr(0x202E)
        + " token=secret-value"
        + chr(10)
        + "done"
    )

    safe = _safe_detail(message)

    assert "window-title" not in safe
    assert "private-data" not in safe
    assert "secret-value" not in safe
    assert "<redacted-secret>" in safe
    assert chr(27) not in safe
    assert all(not unicodedata.category(character).startswith("C") for character in safe)
    assert safe == "bad red c1color bell back next bidi token=<redacted-secret> done"


def test_claude_accepts_exact_separate_pin_with_documented_source_kind() -> None:
    runner = RecordingRunner()
    marketplace = {
        "name": "csaf",
        "source": "git",
        "url": "https://github.com/karthiknambiar/csm-skills-framework.git",
        "ref": "v0.1.0",
    }
    runner.responses = [
        (0, json.dumps([marketplace]).encode(), b""),
        (0, b'[{"name":"csaf@csaf","scope":"user"}]', b""),
    ]

    result = ClaudeAdapterInstaller(Version("0.1.0"), runner=runner).install()

    assert result == AdapterInstallResult(AssistantKind.CLAUDE, None)
    assert len(runner.calls) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended Job Object contract")
def test_windows_timeout_assigns_suspended_parent_before_immediate_child_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = subprocess.Popen
    creation_flags: list[int] = []

    def recording_popen(*args: object, **kwargs: object):
        creation_flags.append(int(kwargs.get("creationflags", 0)))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("csaf.setup.adapters.subprocess.Popen", recording_popen)
    descendants: list[tuple[int, Path]] = []
    started = time.monotonic()
    for attempt in range(6):
        child_pid_path = tmp_path / f"immediate-child-{attempt}.pid"
        delayed_marker = tmp_path / f"immediate-child-{attempt}.survived"
        child_code = (
            f"__import__('pathlib').Path({str(child_pid_path)!r})"
            ".write_text(str(__import__('os').getpid()));"
            "__import__('time').sleep(0.3);"
            f"__import__('pathlib').Path({str(delayed_marker)!r}).write_text('survived');"
            "__import__('time').sleep(4)"
        )
        parent_code = (
            f"__import__('subprocess').Popen([__import__('sys').executable,"
            f"'-c',{child_code!r}]);"
            "__import__('time').sleep(4)"
        )
        with tempfile.TemporaryFile(mode="w+b") as stdout:
            with tempfile.TemporaryFile(mode="w+b") as stderr:
                with pytest.raises(subprocess.TimeoutExpired):
                    _subprocess_runner(
                        [sys.executable, "-c", parent_code],
                        stdout=stdout,
                        stderr=stderr,
                        timeout=0.2,
                    )
        descendants.append((int(child_pid_path.read_text(encoding="utf-8")), delayed_marker))

    assert time.monotonic() - started < 6
    assert len(creation_flags) == 6
    assert all(flags & 0x00000004 for flags in creation_flags)
    for descendant_pid, delayed_marker in descendants:
        assert _pid_stops_within(descendant_pid)
        assert not delayed_marker.exists()
