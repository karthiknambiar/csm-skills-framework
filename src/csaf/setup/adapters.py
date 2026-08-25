"""Native Codex, Claude Code, and Gemini CLI adapter installation boundaries."""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from csaf.office.redaction import redact_officecli_message
from csaf.setup.assets import SetupError
from csaf.setup.types import AssistantKind, Version

_MARKETPLACE_SOURCE = "https://github.com/karthiknambiar/csm-skills-framework.git"
_DEFAULT_TIMEOUT_SECONDS = 60.0
_MAX_CAPTURE_BYTES = 64 * 1024
_CHUNK_SIZE = 16 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True, slots=True)
class AdapterInstallResult:
    """The installed assistant kind and its filesystem target, when known."""

    kind: AssistantKind
    target: Path | None


class AdapterInstaller(Protocol):
    """Common boundary implemented by every native assistant adapter."""

    kind: AssistantKind

    def install(self) -> AdapterInstallResult:
        """Install the adapter and return its recorded target."""


class CommandRunner(Protocol):
    """Injectable binary-stream subprocess runner."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]: ...


def _drain_bounded(source: BinaryIO, destination: BinaryIO) -> None:
    remaining = _MAX_CAPTURE_BYTES + 1
    try:
        while True:
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                return
            if remaining:
                kept = chunk[:remaining]
                destination.write(kept)
                remaining -= len(kept)
    except (OSError, ValueError):
        return


class _WindowsJob:
    """Kill-on-close Windows Job Object for one subprocess tree."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "could not create Windows Job Object")
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "could not configure Windows Job Object")
        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._handle = handle

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        from ctypes import wintypes

        process_handle = wintypes.HANDLE(getattr(process, "_handle"))
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise OSError(
                self._ctypes.get_last_error(),
                "could not assign process to Windows Job Object",
            )

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
    """Resume the suspended primary thread through documented Toolhelp APIs."""
    import ctypes
    from ctypes import wintypes

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry32))
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry32))
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "could not snapshot Windows process threads")
    try:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while available:
            if entry.th32OwnerProcessID == process.pid:
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                if not thread:
                    raise OSError(
                        ctypes.get_last_error(),
                        "could not open suspended Windows process thread",
                    )
                try:
                    if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                        raise OSError(
                            ctypes.get_last_error(),
                            "could not resume suspended Windows process thread",
                        )
                    return
                finally:
                    kernel32.CloseHandle(thread)
            entry.dwSize = ctypes.sizeof(entry)
            available = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        raise OSError(0, "suspended Windows process thread was not found")
    finally:
        kernel32.CloseHandle(snapshot)


def _terminate_process_tree(
    process: subprocess.Popen[bytes], windows_job: _WindowsJob | None
) -> None:
    if os.name == "nt":
        if windows_job is not None:
            windows_job.close()
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                pass


def _finish_readers(
    process: subprocess.Popen[bytes], readers: tuple[threading.Thread, threading.Thread]
) -> None:
    for reader in readers:
        reader.join(timeout=1)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    for reader in readers:
        reader.join(timeout=0.2)


def _subprocess_runner(
    command: Sequence[str],
    *,
    stdout: BinaryIO,
    stderr: BinaryIO,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    popen_options: dict[str, object] = {}
    windows_job: _WindowsJob | None = None
    if os.name == "nt":
        windows_job = _WindowsJob()
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            shell=False,
            **popen_options,
        )
    except BaseException:
        if windows_job is not None:
            windows_job.close()
        raise
    try:
        if windows_job is not None:
            windows_job.assign(process)
            _resume_windows_process(process)
    except BaseException:
        _terminate_process_tree(process, windows_job)
        raise
    assert process.stdout is not None
    assert process.stderr is not None
    readers = (
        threading.Thread(target=_drain_bounded, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=_drain_bounded, args=(process.stderr, stderr), daemon=True),
    )
    for reader in readers:
        reader.start()
    timed_out: subprocess.TimeoutExpired | None = None
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        timed_out = error
        returncode = -1
    finally:
        _terminate_process_tree(process, windows_job)
        _finish_readers(process, readers)
    if timed_out is not None:
        raise timed_out
    return subprocess.CompletedProcess(list(command), returncode)


def _is_link_or_reparse(details: os.stat_result) -> bool:
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _checked_details(path: Path) -> os.stat_result:
    details = path.lstat()
    if _is_link_or_reparse(details):
        raise SetupError("Codex adapter source contains a link or reparse point")
    return details


def _copy_regular_file(source: Path, destination: Path, details: os.stat_result) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != details.st_dev or opened.st_ino != details.st_ino:
            raise SetupError("Codex adapter source changed during installation")
        with os.fdopen(descriptor, "rb", closefd=False) as input_stream:
            with destination.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=_CHUNK_SIZE)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        os.chmod(destination, 0o700 if details.st_mode & 0o111 else 0o600)
    finally:
        os.close(descriptor)


def _copy_skill_tree(source: Path, destination: Path) -> None:
    root_details = _checked_details(source)
    if not stat.S_ISDIR(root_details.st_mode):
        raise SetupError("Codex adapter source is incomplete")
    root = source.resolve(strict=True)
    destination.mkdir(mode=0o700)

    def copy_directory(current: Path, output: Path) -> None:
        current_details = _checked_details(current)
        if not stat.S_ISDIR(current_details.st_mode):
            raise SetupError("Codex adapter source changed during installation")
        resolved = current.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise SetupError("Codex adapter source escaped its verified root") from error
        with os.scandir(current) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            input_path = Path(entry.path)
            details = _checked_details(input_path)
            output_path = output / entry.name
            resolved_entry = input_path.resolve(strict=True)
            try:
                resolved_entry.relative_to(root)
            except ValueError as error:
                raise SetupError("Codex adapter source escaped its verified root") from error
            if stat.S_ISDIR(details.st_mode):
                output_path.mkdir(mode=0o700)
                copy_directory(input_path, output_path)
            elif stat.S_ISREG(details.st_mode):
                _copy_regular_file(input_path, output_path, details)
            else:
                raise SetupError("Codex adapter source contains an unsupported filesystem entry")

    copy_directory(source, destination)


def _open_directory(path: Path) -> int | None:
    if not hasattr(os, "O_DIRECTORY"):
        return None
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))


def _fsync_directory(path: Path) -> None:
    descriptor = _open_directory(path)
    if descriptor is None:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(path: Path) -> None:
    for directory, _children, _files in os.walk(path, topdown=False):
        _fsync_directory(Path(directory))


def _remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


@contextmanager
def _activation_lock(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, bytes(1))
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise SetupError("Codex adapter installation is already in progress") from error
        acquired = True
        yield
    finally:
        try:
            if acquired and os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            elif acquired:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class CodexAdapterInstaller:
    """Install the canonical skill with locked, crash-recoverable activation."""

    kind = AssistantKind.CODEX
    _assistant_name = "Codex"

    def __init__(self, source: Path, skill_root: Path) -> None:
        self._source = Path(source)
        self._skill_root = Path(skill_root)

    def install(self) -> AdapterInstallResult:
        target = self._skill_root / "csaf"
        staging = self._skill_root / ".csaf.staging"
        backup = self._skill_root / ".csaf.backup"
        activated = False
        try:
            self._skill_root.mkdir(parents=True, exist_ok=True)
            with _activation_lock(self._skill_root / ".csaf.install.lock"):
                self._recover(target, staging, backup)
                try:
                    _copy_skill_tree(self._source, staging)
                    if not (staging / "SKILL.md").is_file():
                        raise SetupError(f"{self._assistant_name} adapter source is incomplete")
                    _fsync_tree(staging)
                    _fsync_directory(self._skill_root)
                    if target.exists() or target.is_symlink():
                        os.replace(target, backup)
                        _fsync_directory(self._skill_root)
                    os.replace(staging, target)
                    activated = True
                    _fsync_directory(self._skill_root)
                    if backup.exists() or backup.is_symlink():
                        try:
                            _remove_tree(backup)
                            _fsync_directory(self._skill_root)
                        except OSError as error:
                            raise SetupError(
                                f"{self._assistant_name} adapter activated but cleanup "
                                "is incomplete",
                                activated=True,
                            ) from error
                    return AdapterInstallResult(self.kind, target)
                except SetupError:
                    raise
                except OSError as error:
                    if activated:
                        raise SetupError(
                            f"{self._assistant_name} adapter activated but durability is uncertain",
                            activated=True,
                        ) from error
                    if backup.exists() or backup.is_symlink():
                        try:
                            if target.exists() or target.is_symlink():
                                _remove_tree(target)
                            os.replace(backup, target)
                            _fsync_directory(self._skill_root)
                        except OSError as rollback_error:
                            raise SetupError(
                                f"{self._assistant_name} adapter installation failed and "
                                "the previous adapter could not be restored"
                            ) from rollback_error
                    raise SetupError(
                        f"{self._assistant_name} adapter installation failed"
                    ) from error
                finally:
                    if staging.exists() or staging.is_symlink():
                        try:
                            _remove_tree(staging)
                        except OSError as cleanup_error:
                            raise SetupError(
                                f"{self._assistant_name} adapter cleanup is incomplete",
                                activated=activated,
                            ) from cleanup_error
        except SetupError:
            raise
        except OSError as error:
            message = (
                f"{self._assistant_name} adapter activated but lock release is uncertain"
                if activated
                else f"{self._assistant_name} adapter installation failed"
            )
            raise SetupError(message, activated=activated) from error

    def _recover(self, target: Path, staging: Path, backup: Path) -> None:
        try:
            if staging.exists() or staging.is_symlink():
                _remove_tree(staging)
            if backup.exists() or backup.is_symlink():
                if target.exists() or target.is_symlink():
                    _remove_tree(backup)
                else:
                    os.replace(backup, target)
            _fsync_directory(self._skill_root)
        except OSError as error:
            raise SetupError(f"{self._assistant_name} adapter recovery failed") from error


class GeminiAdapterInstaller(CodexAdapterInstaller):
    """Install the canonical skill into Gemini CLI's user skill directory."""

    kind = AssistantKind.GEMINI
    _assistant_name = "Gemini CLI"

    def install(self) -> AdapterInstallResult:
        try:
            return super().install()
        except SetupError as error:
            message = str(error).replace("Codex adapter", "Gemini CLI adapter")
            if message == str(error):
                raise
            raise SetupError(message, activated=error.activated) from error


class ClaudeAdapterInstaller:
    """Install the versioned Claude marketplace and plugin idempotently."""

    kind = AssistantKind.CLAUDE

    def __init__(
        self,
        version: Version,
        *,
        runner: CommandRunner = _subprocess_runner,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("command timeout must be finite and positive")
        self._version = version
        self._runner = runner
        self._timeout = timeout

    def install(self) -> AdapterInstallResult:
        marketplaces = self._json_list(
            self._run(["claude", "plugin", "marketplace", "list", "--json"], "marketplace list"),
            "marketplace list",
        )
        marketplace_present = self._marketplace_present(marketplaces)
        plugins = self._json_list(
            self._run(["claude", "plugin", "list", "--json"], "plugin list"),
            "plugin list",
        )
        plugin_present = self._plugin_present(plugins)
        if plugin_present and not marketplace_present:
            raise SetupError(
                "Claude Code reports a CSAF plugin without its marketplace",
                activated=True,
            )
        if marketplace_present and plugin_present:
            return AdapterInstallResult(self.kind, None)

        marketplace_added = False
        if not marketplace_present:
            self._run(
                [
                    "claude",
                    "plugin",
                    "marketplace",
                    "add",
                    f"{_MARKETPLACE_SOURCE}#v{self._version}",
                ],
                "marketplace add",
            )
            marketplace_added = True
        try:
            self._run(
                ["claude", "plugin", "install", "csaf@csaf", "--scope", "user"],
                "plugin install",
            )
        except SetupError as install_error:
            if marketplace_added:
                try:
                    self._run(
                        ["claude", "plugin", "marketplace", "remove", "csaf"],
                        "marketplace rollback",
                    )
                except SetupError as rollback_error:
                    raise SetupError(
                        "Claude Code plugin installation failed and the CSAF marketplace "
                        "remains installed",
                        activated=True,
                    ) from rollback_error
            raise install_error
        return AdapterInstallResult(self.kind, None)

    def _run(self, command: list[str], operation: str) -> str:
        try:
            with tempfile.TemporaryFile(mode="w+b") as stdout_stream:
                with tempfile.TemporaryFile(mode="w+b") as stderr_stream:
                    completed = self._runner(
                        command,
                        stdout=stdout_stream,
                        stderr=stderr_stream,
                        timeout=self._timeout,
                    )
                    stdout_stream.seek(0)
                    stderr_stream.seek(0)
                    stdout = stdout_stream.read(_MAX_CAPTURE_BYTES + 1)
                    stderr = stderr_stream.read(_MAX_CAPTURE_BYTES + 1)
            if len(stdout) > _MAX_CAPTURE_BYTES or len(stderr) > _MAX_CAPTURE_BYTES:
                raise SetupError(f"Claude Code {operation} output exceeded the capture limit")
            decoded_stdout = stdout.decode("utf-8", errors="strict")
            decoded_stderr = stderr.decode("utf-8", errors="strict")
        except FileNotFoundError as error:
            raise SetupError("Claude Code executable was not found") from error
        except subprocess.TimeoutExpired as error:
            raise SetupError(
                f"Claude Code {operation} exceeded the {self._timeout:g}s timeout"
            ) from error
        except UnicodeError as error:
            raise SetupError(
                f"Claude Code {operation} returned output that was not valid UTF-8"
            ) from error
        except OSError as error:
            raise SetupError(f"Claude Code {operation} could not be executed") from error

        if completed.returncode != 0:
            detail = decoded_stderr.strip() or decoded_stdout.strip() or "unknown error"
            raise SetupError(
                f"Claude Code {operation} failed with exit code {completed.returncode}: "
                f"{_safe_detail(detail)}"
            )
        return decoded_stdout

    @staticmethod
    def _json_list(output: str, operation: str) -> list[object]:
        try:
            value = json.loads(output)
        except json.JSONDecodeError as error:
            raise SetupError(f"Claude Code {operation} returned invalid JSON") from error
        if not isinstance(value, list):
            raise SetupError(f"Claude Code {operation} returned an unexpected response")
        return value

    def _marketplace_present(self, entries: list[object]) -> bool:
        expected_ref = f"v{self._version}"
        expected_combined = f"{_MARKETPLACE_SOURCE}#{expected_ref}"
        found = False
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise SetupError("Claude Code marketplace list returned an unexpected response")
            name = entry.get("name")
            url = entry.get("url")
            ref = entry.get("ref")
            source = entry.get("source")
            exact_separate = (
                name == "csaf"
                and url == _MARKETPLACE_SOURCE
                and ref == expected_ref
                and source in {None, "git"}
            )
            exact_combined = (
                name == "csaf" and source == expected_combined and url is None and ref is None
            )
            source_collision = url == _MARKETPLACE_SOURCE or (
                isinstance(source, str)
                and (source == _MARKETPLACE_SOURCE or source.startswith(f"{_MARKETPLACE_SOURCE}#"))
            )
            if exact_separate or exact_combined:
                found = True
            elif name == "csaf" or source_collision:
                raise SetupError(
                    "Claude Code CSAF marketplace does not match the exact versioned source"
                )
        return found

    @staticmethod
    def _plugin_present(entries: list[object]) -> bool:
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise SetupError("Claude Code plugin list returned an unexpected response")
            name = entry.get("name") or entry.get("id") or entry.get("plugin")
            marketplace = entry.get("marketplace") or entry.get("source")
            scope = entry.get("scope")
            qualified = name == "csaf@csaf"
            named = name == "csaf" and marketplace in {"csaf", "csaf@csaf"}
            if qualified or named:
                return scope in {None, "user"}
        return False


def _strip_terminal_sequences(message: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(message):
        character = message[index]
        codepoint = ord(character)
        next_character = message[index + 1] if index + 1 < len(message) else ""
        if (character == chr(27) and next_character == "[") or codepoint == 0x9B:
            index += 2 if character == chr(27) else 1
            while index < len(message):
                final = ord(message[index])
                index += 1
                if 0x40 <= final <= 0x7E:
                    break
            continue
        string_control = None
        if character == chr(27) and next_character in {"]", "P", "X", "^", "_"}:
            string_control = 2
        elif codepoint in {0x90, 0x98, 0x9D, 0x9E, 0x9F}:
            string_control = 1
        if string_control is not None:
            index += string_control
            while index < len(message):
                if ord(message[index]) in {0x07, 0x9C}:
                    index += 1
                    break
                if (
                    message[index] == chr(27)
                    and index + 1 < len(message)
                    and message[index + 1] == chr(92)
                ):
                    index += 2
                    break
                index += 1
            continue
        if character == chr(27):
            index += min(2, len(message) - index)
            continue
        output.append(character)
        index += 1
    return "".join(output)


def _safe_detail(message: str) -> str:
    without_terminal_sequences = _strip_terminal_sequences(message)
    printable: list[str] = []
    for character in without_terminal_sequences:
        if character.isspace():
            printable.append(" ")
        elif not unicodedata.category(character).startswith("C"):
            printable.append(character)
    single_line = " ".join("".join(printable).split())
    return redact_officecli_message(single_line[:4096])


def install_adapters(
    detected: Sequence[AssistantKind],
    installers: Mapping[AssistantKind, AdapterInstaller],
    *,
    codex_only: bool = False,
    claude_only: bool = False,
    gemini_only: bool = False,
) -> tuple[AdapterInstallResult, ...]:
    """Install all detected adapters, optionally validating one explicit target."""
    overrides = {
        AssistantKind.CODEX: codex_only,
        AssistantKind.CLAUDE: claude_only,
        AssistantKind.GEMINI: gemini_only,
    }
    if sum(overrides.values()) > 1:
        raise SetupError("assistant-only overrides cannot be used together")
    detected_set = set(detected)
    requested = next((kind for kind, selected in overrides.items() if selected), None)
    if requested is not None and requested not in detected_set:
        raise SetupError("requested assistant was not detected")
    selected = tuple(
        kind
        for kind in AssistantKind
        if kind in detected_set and (requested is None or kind is requested)
    )
    results: list[AdapterInstallResult] = []
    for kind in selected:
        installer = installers.get(kind)
        if installer is None:
            raise SetupError(f"{kind.value} adapter installer is unavailable")
        result = installer.install()
        if result.kind is not kind:
            raise SetupError("adapter installer returned an unexpected assistant kind")
        results.append(result)
    return tuple(results)
