#!/usr/bin/env python3
"""Scan repository content for credential-shaped secrets without printing them."""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 1_048_576
GIT_TIMEOUT_SECONDS = 30
MAX_DISPLAY_PATH_CHARS = 240
TRUNCATION_MARKER = "...[truncated]"
SKIPPED_PARTS = frozenset({".git", ".venv", ".worktrees", "__pycache__", "node_modules"})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str
    commit: str | None = None


class SecretScanError(RuntimeError):
    """A sanitized scanner failure safe to report to a user."""


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
            r"\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{20,}"
        ),
    ),
)


def scan_text(path: str, text: str, commit: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for category, pattern in PATTERNS:
            if pattern.search(line):
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


def _text_from_bytes(content: bytes) -> str | None:
    if len(content) > MAX_FILE_BYTES or b"\x00" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _scan_content(path: str, content: bytes, commit: str | None = None) -> list[Finding]:
    if _skip_path(path):
        return []
    text = _text_from_bytes(content)
    return [] if text is None else scan_text(path, text, commit)


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


def _worktree_findings(repo: Path) -> list[Finding]:
    names = _git(repo, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    findings: list[Finding] = []
    for raw_name in names.split(b"\x00"):
        if not raw_name:
            continue
        path = _decode_path(raw_name)
        content = _read_worktree_file(repo, path)
        if content is not None:
            findings.extend(_scan_content(path, content))
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


def _rebind_matches(matches: BlobMatches, path: str, commit: str | None = None) -> list[Finding]:
    return [Finding(path, line, category, commit) for line, category in matches]


def _tracked_findings(repo: Path) -> list[Finding]:
    entries = _git(repo, ["ls-files", "--stage", "-z"])
    findings: list[Finding] = []
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
        findings.extend(_rebind_matches(matches, path))
    return findings


def _history_findings(repo: Path) -> list[Finding]:
    commits = _git(repo, ["rev-list", "--all"]).decode("ascii").splitlines()
    findings: list[Finding] = []
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
            findings.extend(_rebind_matches(matches, path, commit))
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
) -> list[Finding]:
    findings: list[Finding] = []
    if worktree:
        findings.extend(_worktree_findings(repo))
    if tracked:
        findings.extend(_tracked_findings(repo))
    if history:
        findings.extend(_history_findings(repo))
    return _ordered_unique(findings)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", action="store_true", help="scan tracked and untracked files")
    parser.add_argument("--tracked", action="store_true", help="scan content in the Git index")
    parser.add_argument("--history", action="store_true", help="scan all reachable Git history")
    return parser


def main(argv: Sequence[str] | None = None, *, repo: Path | None = None) -> int:
    args = _parser().parse_args(argv)
    if not (args.worktree or args.tracked or args.history):
        print("secret scan failed: select at least one scan mode", file=sys.stderr)
        return 2
    try:
        findings = scan_repository(
            (repo or Path.cwd()).resolve(),
            worktree=args.worktree,
            tracked=args.tracked,
            history=args.history,
        )
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
