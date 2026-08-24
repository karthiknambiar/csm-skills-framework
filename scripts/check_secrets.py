#!/usr/bin/env python3
"""Scan repository content for credential-shaped secrets without printing them."""

from __future__ import annotations

import argparse
import codecs
import io
import os
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

MAX_FILE_BYTES = 1_048_576
MAX_PACKAGE_BYTES = 268_435_456
MAX_ARCHIVE_MEMBER_BYTES = 67_108_864
MAX_ARCHIVE_TOTAL_BYTES = 536_870_912
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_DEPTH = 3
MAX_ARCHIVE_PATH_CHARS = 1_024
ARCHIVE_SUFFIXES = (".zip", ".whl")
ARCHIVE_READ_CHUNK_BYTES = 65_536
ARCHIVE_SCAN_OVERLAP_CHARS = 4_096
ARCHIVE_SPOOL_MEMORY_BYTES = 1_048_576
MAX_ARCHIVE_COMPRESSED_BYTES = 536_870_912
GIT_TIMEOUT_SECONDS = 30
MAX_DISPLAY_PATH_CHARS = 240
MAX_FINDINGS = 10_000
TRUNCATION_MARKER = "...[truncated]"
SKIPPED_PARTS = frozenset({".git", ".venv", ".worktrees", "__pycache__", "node_modules"})
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_WINDOWS_INVALID_CHARS = frozenset('<>":|?*')


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str
    commit: str | None = None


class SecretScanError(RuntimeError):
    """A sanitized scanner failure safe to report to a user."""


@dataclass
class _FindingBudget:
    count: int = 0

    def reserve(self, amount: int = 1) -> None:
        updated = self.count + amount
        if updated > MAX_FINDINGS:
            raise SecretScanError("finding limit exceeded")
        self.count = updated


@dataclass
class _ArchiveBudget:
    members: int = 0
    bytes: int = 0
    compressed_bytes: int = 0

    def reserve(self, size: int, compressed_size: int) -> None:
        self.members += 1
        self.bytes += size
        self.compressed_bytes += compressed_size
        if (
            self.members > MAX_ARCHIVE_MEMBERS
            or self.bytes > MAX_ARCHIVE_TOTAL_BYTES
            or self.compressed_bytes > MAX_ARCHIVE_COMPRESSED_BYTES
        ):
            raise SecretScanError("archive scan limit exceeded")


PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    (
        "github_token",
        re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{82}(?![A-Za-z0-9_])"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b")),
    (
        "stripe_key",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{24,}"
            r"(?![A-Za-z0-9_])"
        ),
    ),
    (
        "provider_credential",
        re.compile(
            r"(?i)\b(?:[A-Z0-9]+[_-])?(?:API[_-]?KEY|ACCESS[_-]?TOKEN|AUTH[_-]?TOKEN)"
            r"\s*[:=]\s*(?:[\"'][A-Za-z0-9_./+=-]{20,}[\"']|[A-Za-z0-9_/+=-]{20,})"
        ),
    ),
)


def scan_text(
    path: str,
    text: str,
    commit: str | None = None,
    *,
    _finding_budget: _FindingBudget | None = None,
) -> list[Finding]:
    budget = _finding_budget or _FindingBudget()
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for category, pattern in PATTERNS:
            if pattern.search(line):
                budget.reserve()
                findings.append(Finding(path, line_number, category, commit))
    return findings


def _redact_credentials(value: str) -> str:
    redacted = value
    for _, pattern in PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _escape_controls(value: str) -> str:
    escaped: list[str] = []
    for character in value:
        if unicodedata.category(character).startswith("C"):
            codepoint = ord(character)
            width = 4 if codepoint <= 0xFFFF else 8
            prefix = "u" if width == 4 else "U"
            escaped.append(f"\\{prefix}{codepoint:0{width}x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def _sanitize_location(path: str) -> str:
    safe = _escape_controls(_redact_credentials(path))
    if len(safe) > MAX_DISPLAY_PATH_CHARS:
        safe = safe[: MAX_DISPLAY_PATH_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return safe


def _sanitize_commit(commit: str | None) -> str | None:
    if commit is None:
        return None
    return commit.lower() if re.fullmatch(r"[0-9a-fA-F]{7,64}", commit) else "invalid"


def render(findings: Iterable[Finding]) -> str:
    lines = []
    for finding in findings:
        category = _escape_controls(_redact_credentials(finding.category))
        location = _sanitize_location(finding.path)
        commit = _sanitize_commit(finding.commit)
        suffix = f" (commit {commit})" if commit else ""
        line = str(finding.line) if type(finding.line) is int and finding.line > 0 else "invalid"
        lines.append(f"{category}: {location}:{line}{suffix}")
    return "\n".join(lines)


def _git(repo: Path, args: Sequence[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretScanError("git command unavailable") from exc
    if result.returncode != 0:
        raise SecretScanError("git command failed")
    return result.stdout


def _decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def _skip_path(path: str) -> bool:
    return any(part in SKIPPED_PARTS for part in PurePosixPath(path).parts)


def _text_from_bytes(content: bytes, *, max_bytes: int = MAX_FILE_BYTES) -> str | None:
    if len(content) > max_bytes or b"\x00" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _archive_member_parts(name: str) -> tuple[str, ...]:
    if (
        not name
        or len(name) > MAX_ARCHIVE_PATH_CHARS
        or "\x00" in name
        or "\\" in name
        or PurePosixPath(name).is_absolute()
        or any(
            unicodedata.category(character).startswith("C") or character in _WINDOWS_INVALID_CHARS
            for character in name
        )
    ):
        raise SecretScanError("archive member path invalid")
    parts = name.rstrip("/").split("/")
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.endswith((".", " ")) for part in parts)
        or any(part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES for part in parts)
    ):
        raise SecretScanError("archive member path invalid")
    return tuple(parts)


def _archive_info_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode) if mode else False


_LINE_BREAK = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


class _StreamingTextScanner:
    """Incrementally decode and scan text with bounded cross-chunk overlap."""

    def __init__(
        self,
        path: str,
        commit: str | None,
        finding_budget: _FindingBudget | None = None,
    ) -> None:
        self.path = path
        self.commit = commit
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self.overlap = ""
        self.overlap_line = 1
        self.findings: set[Finding] = set()
        self.finding_budget = finding_budget or _FindingBudget()
        self.is_text = True

    def feed(self, chunk: bytes) -> None:
        if not self.is_text:
            return
        if b"\x00" in chunk:
            self.is_text = False
            self.findings.clear()
            return
        try:
            decoded = self.decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            self.is_text = False
            self.findings.clear()
            return
        self._scan(decoded)

    def finish(self) -> list[Finding]:
        if not self.is_text:
            return []
        try:
            self._scan(self.decoder.decode(b"", final=True), final=True)
        except UnicodeDecodeError:
            self.findings.clear()
            return []
        ordered = sorted(self.findings, key=lambda item: (item.line, item.category))
        if len(ordered) > MAX_FINDINGS:
            raise SecretScanError("finding limit exceeded")
        self.finding_budget.reserve(len(ordered))
        return ordered

    def _scan(self, decoded: str, *, final: bool = False) -> None:
        window = self.overlap + decoded
        drop = len(window) if final else max(0, len(window) - ARCHIVE_SCAN_OVERLAP_CHARS)
        overlap_limit = ARCHIVE_SCAN_OVERLAP_CHARS
        if not final and drop and drop < len(window) and window[drop - 1 : drop + 1] == "\r\n":
            drop -= 1
            overlap_limit += 1
        line_break_ends = [match.end() for match in _LINE_BREAK.finditer(window)]
        matches = [
            (category, match)
            for category, pattern in PATTERNS
            for match in pattern.finditer(window)
        ]
        if not final:
            deferred_starts = [match.start() for _, match in matches if match.end() == len(window)]
            if deferred_starts:
                drop = min(drop, min(deferred_starts))
            if len(window) - drop > overlap_limit:
                raise SecretScanError("streaming scan overlap limit exceeded")
        for category, match in matches:
            if not final and match.end() == len(window):
                continue
            line = self.overlap_line + bisect_right(line_break_ends, match.start())
            finding = Finding(self.path, line, category, self.commit)
            if finding not in self.findings and len(self.findings) <= MAX_FINDINGS:
                self.findings.add(finding)
        if final:
            self.overlap = ""
            return
        self.overlap_line += bisect_right(line_break_ends, drop)
        self.overlap = window[drop:]


def _scan_archive_source(
    path: str,
    source: str | os.PathLike[str] | BinaryIO,
    *,
    commit: str | None,
    depth: int,
    budget: _ArchiveBudget,
    finding_budget: _FindingBudget,
) -> list[Finding]:
    if depth > MAX_ARCHIVE_DEPTH:
        raise SecretScanError("archive nesting limit exceeded")
    try:
        with zipfile.ZipFile(source) as archive:
            findings: list[Finding] = []
            normalized_names: set[str] = set()
            for info in archive.infolist():
                parts = _archive_member_parts(info.filename)
                key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
                if key in normalized_names:
                    raise SecretScanError("archive member path invalid")
                normalized_names.add(key)
                if info.flag_bits & 0x1 or _archive_info_is_symlink(info):
                    raise SecretScanError("unsupported archive member")
                if info.file_size < 0 or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise SecretScanError("archive member size limit exceeded")
                if info.compress_size < 0:
                    raise SecretScanError("archive member size invalid")
                budget.reserve(info.file_size, info.compress_size)
                if info.is_dir():
                    continue
                member_path = f"{path}!{info.filename}"
                scanner = _StreamingTextScanner(member_path, commit, finding_budget)
                with tempfile.SpooledTemporaryFile(
                    max_size=ARCHIVE_SPOOL_MEMORY_BYTES, mode="w+b"
                ) as spool:
                    total = 0
                    try:
                        with archive.open(info) as member_file:
                            while True:
                                chunk = member_file.read(ARCHIVE_READ_CHUNK_BYTES)
                                if not chunk:
                                    break
                                total += len(chunk)
                                if total > info.file_size:
                                    raise SecretScanError("archive member size invalid")
                                scanner.feed(chunk)
                                spool.write(chunk)
                    except SecretScanError:
                        raise
                    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                        raise SecretScanError("archive member could not be read") from exc
                    if total != info.file_size:
                        raise SecretScanError("archive member size invalid")
                    spool.seek(0)
                    is_archive = info.filename.lower().endswith(
                        ARCHIVE_SUFFIXES
                    ) or zipfile.is_zipfile(spool)
                    spool.seek(0)
                    if is_archive:
                        findings.extend(
                            _scan_archive_source(
                                member_path,
                                spool,
                                commit=commit,
                                depth=depth + 1,
                                budget=budget,
                                finding_budget=finding_budget,
                            )
                        )
                    else:
                        findings.extend(scanner.finish())
            return findings
    except SecretScanError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise SecretScanError("archive could not be scanned") from exc


def _scan_content(
    path: str,
    content: bytes,
    commit: str | None = None,
    *,
    _finding_budget: _FindingBudget | None = None,
) -> list[Finding]:
    if _skip_path(path):
        return []
    finding_budget = _finding_budget or _FindingBudget()
    if path.lower().endswith(ARCHIVE_SUFFIXES):
        return _scan_archive_source(
            path,
            io.BytesIO(content),
            commit=commit,
            depth=0,
            budget=_ArchiveBudget(),
            finding_budget=finding_budget,
        )
    text = _text_from_bytes(content)
    return [] if text is None else scan_text(path, text, commit, _finding_budget=finding_budget)


def scan_package(path: Path, *, _finding_budget: _FindingBudget | None = None) -> list[Finding]:
    """Scan one regular package file without extracting or executing its content."""
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or _is_reparse_point(before)
            or before.st_size > MAX_PACKAGE_BYTES
        ):
            raise SecretScanError("package file invalid")
        findings = _scan_archive_source(
            path.name,
            path,
            commit=None,
            depth=0,
            budget=_ArchiveBudget(),
            finding_budget=_finding_budget or _FindingBudget(),
        )
        after = path.lstat()
    except SecretScanError:
        raise
    except OSError as exc:
        raise SecretScanError("package file could not be read") from exc
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise SecretScanError("package file size changed")
    return _ordered_unique(findings)


def _safe_git_parts(path: str) -> tuple[str, ...] | None:
    if not path or "\x00" in path:
        return None
    if PurePosixPath(path).is_absolute():
        return None
    raw_parts = path.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return None
    return tuple(raw_parts)


def _is_reparse_point(info: object) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _read_worktree_file(repo: Path, path: str) -> bytes | None:
    parts = _safe_git_parts(path)
    if parts is None or _skip_path(path):
        return None
    if os.name == "nt" and any("\\" in part for part in parts):
        return None
    try:
        repo_root = repo.resolve(strict=True)
        candidate = repo_root
        for index, part in enumerate(parts):
            candidate = candidate / part
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                return None
            is_last = index == len(parts) - 1
            if is_last and not stat.S_ISREG(info.st_mode):
                return None
            if not is_last and not stat.S_ISDIR(info.st_mode):
                return None
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(repo_root)
        except ValueError:
            return None
        if info.st_size > MAX_FILE_BYTES:
            return None
        return candidate.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SecretScanError("unable to read repository file") from exc


def _worktree_findings(repo: Path, finding_budget: _FindingBudget | None = None) -> list[Finding]:
    names = _git(repo, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    findings: list[Finding] = []
    budget = finding_budget or _FindingBudget()
    for raw_name in names.split(b"\x00"):
        if not raw_name:
            continue
        path = _decode_path(raw_name)
        content = _read_worktree_file(repo, path)
        if content is not None:
            findings.extend(_scan_content(path, content, _finding_budget=budget))
    return findings


BlobMatches = tuple[tuple[int, str], ...]


def _content_matches(content: bytes) -> BlobMatches:
    text = _text_from_bytes(content)
    if text is None:
        return ()
    return tuple((finding.line, finding.category) for finding in scan_text("", text))


def _parse_blob_size(raw_size: bytes) -> int:
    try:
        size = int(raw_size.strip())
    except (TypeError, ValueError) as exc:
        raise SecretScanError("git object metadata invalid") from exc
    if size < 0:
        raise SecretScanError("git object metadata invalid")
    return size


def _load_blob_matches(
    repo: Path,
    object_id: str,
    cache: dict[str, BlobMatches],
    *,
    known_size: int | None = None,
) -> BlobMatches:
    if object_id in cache:
        return cache[object_id]
    size = known_size
    if size is None:
        size = _parse_blob_size(_git(repo, ["cat-file", "-s", object_id]))
    if size > MAX_FILE_BYTES:
        cache[object_id] = ()
        return ()
    matches = _content_matches(_git(repo, ["cat-file", "blob", object_id]))
    cache[object_id] = matches
    return matches


def _rebind_matches(
    matches: BlobMatches,
    path: str,
    commit: str | None = None,
    *,
    finding_budget: _FindingBudget | None = None,
) -> list[Finding]:
    budget = finding_budget or _FindingBudget()
    findings: list[Finding] = []
    for line, category in matches:
        budget.reserve()
        findings.append(Finding(path, line, category, commit))
    return findings


def _tracked_findings(repo: Path, finding_budget: _FindingBudget | None = None) -> list[Finding]:
    entries = _git(repo, ["ls-files", "--stage", "-z"])
    findings: list[Finding] = []
    budget = finding_budget or _FindingBudget()
    cache: dict[str, BlobMatches] = {}
    for raw_entry in entries.split(b"\x00"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
        except ValueError as exc:
            raise SecretScanError("git index metadata invalid") from exc
        fields = metadata.split()
        if len(fields) != 3 or fields[2] != b"0":
            continue
        path = _decode_path(raw_path)
        if _safe_git_parts(path) is None or _skip_path(path):
            continue
        try:
            object_id = fields[1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise SecretScanError("git index metadata invalid") from exc
        matches = _load_blob_matches(repo, object_id, cache)
        findings.extend(_rebind_matches(matches, path, finding_budget=budget))
    return findings


def _history_findings(repo: Path, finding_budget: _FindingBudget | None = None) -> list[Finding]:
    commits = _git(repo, ["rev-list", "--all"]).decode("ascii").splitlines()
    findings: list[Finding] = []
    budget = finding_budget or _FindingBudget()
    cache: dict[str, BlobMatches] = {}
    for commit in commits:
        entries = _git(repo, ["ls-tree", "-r", "-l", "-z", "--full-tree", commit])
        for raw_entry in entries.split(b"\x00"):
            if not raw_entry:
                continue
            try:
                metadata, raw_path = raw_entry.split(b"\t", 1)
            except ValueError as exc:
                raise SecretScanError("git history metadata invalid") from exc
            fields = metadata.split()
            if len(fields) != 4 or fields[1] != b"blob" or fields[3] == b"-":
                continue
            path = _decode_path(raw_path)
            if _safe_git_parts(path) is None or _skip_path(path):
                continue
            try:
                object_id = fields[2].decode("ascii")
            except UnicodeDecodeError as exc:
                raise SecretScanError("git history metadata invalid") from exc
            size = _parse_blob_size(fields[3])
            matches = _load_blob_matches(repo, object_id, cache, known_size=size)
            findings.extend(_rebind_matches(matches, path, commit, finding_budget=budget))
    return findings


def _ordered_unique(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(
        set(findings),
        key=lambda item: (item.path, item.line, item.category, item.commit or ""),
    )


def scan_repository(
    repo: Path,
    *,
    worktree: bool,
    tracked: bool,
    history: bool,
    _finding_budget: _FindingBudget | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    budget = _finding_budget or _FindingBudget()
    if worktree:
        findings.extend(_worktree_findings(repo, budget))
    if tracked:
        findings.extend(_tracked_findings(repo, budget))
    if history:
        findings.extend(_history_findings(repo, budget))
    return _ordered_unique(findings)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", action="store_true", help="scan tracked and untracked files")
    parser.add_argument("--tracked", action="store_true", help="scan content in the Git index")
    parser.add_argument("--history", action="store_true", help="scan all reachable Git history")
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        metavar="PATH",
        help="scan a ZIP or wheel package; repeat for multiple artifacts",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, repo: Path | None = None) -> int:
    args = _parser().parse_args(argv)
    if not (args.worktree or args.tracked or args.history or args.package):
        print("secret scan failed: select at least one scan mode", file=sys.stderr)
        return 2
    try:
        repository = (repo or Path.cwd()).resolve()
        finding_budget = _FindingBudget()
        findings = scan_repository(
            repository,
            worktree=args.worktree,
            tracked=args.tracked,
            history=args.history,
            _finding_budget=finding_budget,
        )
        for raw_path in args.package:
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = repository / candidate
            candidate = Path(os.path.abspath(candidate))
            try:
                candidate.relative_to(repository)
            except ValueError as exc:
                raise SecretScanError("package path outside repository") from exc
            findings.extend(scan_package(candidate, _finding_budget=finding_budget))
        findings = _ordered_unique(findings)
    except SecretScanError:
        print("secret scan failed: repository scan could not be completed", file=sys.stderr)
        return 2
    if findings:
        print(render(findings))
        return 1
    print("secret scan clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
