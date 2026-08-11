from __future__ import annotations

import dataclasses
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.check_secrets as secret_scan
from scripts.check_secrets import (
    MAX_FILE_BYTES,
    Finding,
    _history_findings,
    _read_worktree_file,
    _tracked_findings,
    main,
    render,
    scan_repository,
    scan_text,
)


def _secret(prefix: str, tail: str) -> str:
    """Build dummy signatures at runtime so fixtures never resemble live credentials."""
    return prefix + tail


@pytest.mark.parametrize(
    ("value", "category"),
    [
        (_secret("sk-", "dummyopenai0123456789ABCDE"), "openai_key"),
        (_secret("ghp_", "DummyGithubToken012345678901234567890123"), "github_token"),
        (_secret("github_" + "pat_", "G" * 82), "github_token"),
        (_secret("AKIA", "DUMMYAWSKEY12345"), "aws_access_key"),
        (_secret("AIza", "D" * 35), "google_api_key"),
        (_secret("xoxb-", "123456789012-DummySlackToken1234567890"), "slack_token"),
        (_secret("sk_" + "live_", "DummyStripeKey012345678901234"), "stripe_key"),
        (_secret("rk_" + "live_", "R" * 24), "stripe_key"),
        (_secret("rk_" + "test_", "T" * 24), "stripe_key"),
        ("SERVICE_API_KEY=" + "DummyProviderCredential012345", "provider_credential"),
    ],
)
def test_scan_text_detects_credential_shapes_with_line_numbers(value: str, category: str) -> None:
    findings = scan_text("config/settings.txt", f"safe\n{value}\n")

    assert findings == [Finding("config/settings.txt", 2, category)]


def test_scan_text_detects_private_key_header_but_not_generic_terms() -> None:
    header = "-----BEGIN " + "PRIVATE KEY-----"

    generic_terms = "Set API_KEY in your environment.\nAPI key is optional here.\n"
    assert scan_text("README.md", generic_terms) == []
    assert scan_text("identity.pem", f"note\n{header}\n") == [
        Finding("identity.pem", 2, "private_key")
    ]


def test_modern_token_boundaries_reject_short_or_embedded_prose() -> None:
    near_misses = "\n".join(
        [
            _secret("github_" + "pat_", "G" * 59),
            _secret("rk_" + "live_", "R" * 23),
            _secret("rk_" + "test_", "T" * 23),
            "documentationgithub_" + "pat_" + "G" * 82,
            "prefixrk_" + "live_" + "R" * 24,
            "Use github_pat_ or rk_live_ as explanatory prefixes only.",
        ]
    )

    assert scan_text("docs/security.md", near_misses) == []


def test_modern_token_rendering_redacts_values() -> None:
    github = _secret("github_" + "pat_", "G" * 82)
    stripe = _secret("rk_" + "live_", "R" * 24)

    findings = scan_text("secrets.txt", f"{github}\n{stripe}\n")
    output = render(findings)

    assert findings == [
        Finding("secrets.txt", 1, "github_token"),
        Finding("secrets.txt", 2, "stripe_key"),
    ]
    assert github not in output
    assert stripe not in output


def test_finding_is_frozen_and_render_never_includes_secret_material() -> None:
    finding = Finding("folder with spaces/config.txt", 7, "github_token", "abc1234")
    secret = _secret("ghp_", "DoNotPrintThisDummyToken123456789012345")

    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.line = 8  # type: ignore[misc]

    output = render([finding])
    assert output == "github_token: folder with spaces/config.txt:7 (commit abc1234)"
    assert secret not in output


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "security-tests@example.invalid")
    _git(repo, "config", "user.name", "Security Tests")


def test_repository_modes_are_deterministic_deduplicated_and_safe(tmp_path: Path) -> None:
    repo = tmp_path / "repo with spaces"
    _init_repo(repo)
    tracked_secret = _secret("ghp_", "TrackedDummyToken0123456789012345678901")
    untracked_secret = _secret("xoxb-", "123456789012-UntrackedDummyToken12345")
    tracked = repo / "tracked config.txt"
    tracked.write_text(tracked_secret + "\n", encoding="utf-8")
    _git(repo, "add", "tracked config.txt")
    _git(repo, "commit", "--quiet", "-m", "add tracked fixture")
    (repo / "untracked config.txt").write_text(untracked_secret + "\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "ignored.txt").write_text(tracked_secret, encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"\x00" + tracked_secret.encode())
    (repo / "large.txt").write_bytes(b"x" * (1_048_576 + 1) + tracked_secret.encode())

    findings = scan_repository(repo, worktree=True, tracked=True, history=False)

    assert findings == [
        Finding("tracked config.txt", 1, "github_token"),
        Finding("untracked config.txt", 1, "slack_token"),
    ]
    assert scan_repository(repo, worktree=True, tracked=True, history=False) == findings


def test_tracked_scans_index_and_history_scans_removed_content(tmp_path: Path) -> None:
    repo = tmp_path / "history repo"
    _init_repo(repo)
    secret = _secret("AKIA", "HISTORYDUMMY1234")
    path = repo / "old credential.txt"
    path.write_text(secret + "\n", encoding="utf-8")
    _git(repo, "add", "old credential.txt")
    _git(repo, "commit", "--quiet", "-m", "historical fixture")
    secret_commit = _git(repo, "rev-parse", "HEAD")
    path.write_text("clean working tree\n", encoding="utf-8")

    assert scan_repository(repo, worktree=False, tracked=True, history=False) == [
        Finding("old credential.txt", 1, "aws_access_key")
    ]

    path.unlink()
    _git(repo, "add", "--update", "old credential.txt")
    _git(repo, "commit", "--quiet", "-m", "remove fixture")
    history = scan_repository(repo, worktree=False, tracked=False, history=True)
    assert Finding("old credential.txt", 1, "aws_access_key", secret_commit) in history
    assert secret not in render(history)


def test_main_exit_codes_and_failure_output_are_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = tmp_path / "clean"
    _init_repo(clean)
    (clean / "README.md").write_text("API_KEY documentation only\n", encoding="utf-8")
    _git(clean, "add", "README.md")
    _git(clean, "commit", "--quiet", "-m", "clean")
    assert main(["--worktree"], repo=clean) == 0

    secret = _secret("sk-", "DummyOpenAIKey0123456789012345")
    (clean / "config.txt").write_text(secret, encoding="utf-8")
    assert main(["--worktree"], repo=clean) == 1
    assert secret not in capsys.readouterr().out

    not_repo = tmp_path / "not-a-repo"
    not_repo.mkdir()
    marker = _secret("ghp_", "FailureMustNotLeak012345678901234567")
    assert main(["--tracked"], repo=not_repo) == 2
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err
    assert "secret scan failed" in captured.err.lower()


def test_cli_requires_at_least_one_mode() -> None:
    assert main([], repo=Path.cwd()) == 2


def test_git_timeout_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = _secret("ghp_", "TimeoutMustNotLeak012345678901234567890")

    def time_out(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(["git"], 30, output=secret.encode(), stderr=secret.encode())

    monkeypatch.setattr(secret_scan.subprocess, "run", time_out)

    assert main(["--tracked"], repo=tmp_path) == 2
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "secret scan failed" in captured.err.lower()


def test_committed_test_source_contains_no_credential_shaped_fixture() -> None:
    source = Path(__file__).read_text(encoding="utf-8")

    assert scan_text(Path(__file__).as_posix(), source) == []


def test_repository_ignores_common_credential_files_but_keeps_env_example() -> None:
    root = Path(__file__).parents[2]
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            ".env",
            ".env.local",
            "identity.pem",
            "identity.key",
            "credentials-production.json",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    example = subprocess.run(
        ["git", "check-ignore", "--no-index", ".env.example"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0
    assert len(ignored.stdout.splitlines()) == 5
    assert example.returncode == 1


def test_ci_fetches_history_and_scans_before_tests() -> None:
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "fetch-depth: 0" in workflow
    scan = "python scripts/check_secrets.py --worktree --tracked --history"
    assert scan in workflow
    assert workflow.index(scan) < workflow.index("pytest")


def test_render_sanitizes_secret_controls_commit_and_excessive_path() -> None:
    secret = _secret("github_" + "pat_", "P" * 82)
    malicious_path = "folder/" + secret + "\n\r\t\x1b\u202e.txt" + ("x" * 500)
    finding = Finding(
        malicious_path,
        "9\nline-spoof",  # type: ignore[arg-type]
        "github_token\ncategory-spoof",
        "abc123\ncommit-spoof",
    )

    output = render([finding])

    assert len(output.splitlines()) == 1
    assert secret not in output
    assert "[REDACTED]" in output
    assert "\\u000a" in output
    assert "\\u000d" in output
    assert "\\u0009" in output
    assert "\\u001b" in output
    assert "\\u202e" in output
    assert "[truncated]" in output
    assert "line-spoof" not in output
    assert "commit-spoof" not in output
    assert "category-spoof" in output
    assert "commit invalid" in output


def test_worktree_reader_rejects_symlink_and_outside_paths_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    candidate = repo / "loop"
    original_lstat = Path.lstat
    original_read_bytes = Path.read_bytes

    def fake_lstat(path: Path) -> SimpleNamespace:
        if path == candidate:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK | 0o777,
                st_size=0,
                st_file_attributes=0,
            )
        return original_lstat(path)  # type: ignore[return-value]

    def fail_if_read(path: Path) -> bytes:
        if path == candidate:
            raise AssertionError("symlink content must never be read")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(Path, "read_bytes", fail_if_read)

    assert _read_worktree_file(repo, "loop") is None
    assert _read_worktree_file(repo, "../outside.txt") is None
    assert _read_worktree_file(repo, str((tmp_path / "outside.txt").resolve())) is None


def test_tracked_skips_oversized_blob_before_content_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    object_id = "a" * 40
    calls: list[tuple[str, ...]] = []

    def fake_git(repo: Path, args: list[str]) -> bytes:
        calls.append(tuple(args))
        if args == ["ls-files", "--stage", "-z"]:
            return f"100644 {object_id} 0\tlarge file.txt\0".encode()
        if args == ["cat-file", "-s", object_id]:
            return f"{MAX_FILE_BYTES + 1}\n".encode()
        raise AssertionError(f"unexpected Git call: {args!r}")

    monkeypatch.setattr(secret_scan, "_git", fake_git)

    assert _tracked_findings(tmp_path) == []
    assert ("cat-file", "blob", object_id) not in calls


def test_history_skips_oversized_blob_before_content_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = "b" * 40
    object_id = "c" * 40
    calls: list[tuple[str, ...]] = []

    def fake_git(repo: Path, args: list[str]) -> bytes:
        calls.append(tuple(args))
        if args == ["rev-list", "--all"]:
            return f"{commit}\n".encode()
        if args == ["ls-tree", "-r", "-l", "-z", "--full-tree", commit]:
            return f"100644 blob {object_id} {MAX_FILE_BYTES + 1}\tlarge.txt\0".encode()
        raise AssertionError(f"unexpected Git call: {args!r}")

    monkeypatch.setattr(secret_scan, "_git", fake_git)

    assert _history_findings(tmp_path) == []
    assert ("cat-file", "blob", object_id) not in calls


def test_history_fetches_and_scans_each_unique_blob_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_commit = "1" * 40
    second_commit = "2" * 40
    object_id = "d" * 40
    secret = _secret("AKIA", "CACHEDUMMY123456")
    blob_calls = 0

    def fake_git(repo: Path, args: list[str]) -> bytes:
        nonlocal blob_calls
        if args == ["rev-list", "--all"]:
            return f"{first_commit}\n{second_commit}\n".encode()
        if args[:5] == ["ls-tree", "-r", "-l", "-z", "--full-tree"]:
            path = "first.txt" if args[5] == first_commit else "second.txt"
            return f"100644 blob {object_id} {len(secret)}\t{path}\0".encode()
        if args == ["cat-file", "blob", object_id]:
            blob_calls += 1
            return secret.encode()
        raise AssertionError(f"unexpected Git call: {args!r}")

    monkeypatch.setattr(secret_scan, "_git", fake_git)

    assert _history_findings(tmp_path) == [
        Finding("first.txt", 1, "aws_access_key", first_commit),
        Finding("second.txt", 1, "aws_access_key", second_commit),
    ]
    assert blob_calls == 1


def test_tracked_preserves_literal_backslashes_but_skips_posix_internal_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    object_id = "e" * 40
    skipped_internal = "f" * 40
    skipped_traversal = "a" * 40
    secret = _secret("github_" + "pat_", "Q" * 82)
    literal_name = ".venv\\secret.txt"

    def fake_git(repo: Path, args: list[str]) -> bytes:
        if args == ["ls-files", "--stage", "-z"]:
            entries = [
                f"100644 {object_id} 0\t{literal_name}",
                f"100644 {skipped_internal} 0\t.venv/secret.txt",
                f"100644 {skipped_traversal} 0\t../secret.txt",
            ]
            return ("\0".join(entries) + "\0").encode()
        if args == ["cat-file", "-s", object_id]:
            return str(len(secret)).encode()
        if args == ["cat-file", "blob", object_id]:
            return secret.encode()
        raise AssertionError(f"unsafe or unexpected Git call: {args!r}")

    monkeypatch.setattr(secret_scan, "_git", fake_git)

    findings = _tracked_findings(tmp_path)
    assert findings == [Finding(literal_name, 1, "github_token")]
    assert literal_name in render(findings)


def test_history_preserves_literal_backslash_dotdot_and_drive_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = "3" * 40
    object_id = "4" * 40
    skipped_internal = "5" * 40
    skipped_traversal = "6" * 40
    secret = _secret("AKIA", "BACKSLASHX123456")
    literal_names = ("..\\secret.txt", "C:\\secret.txt")

    def fake_git(repo: Path, args: list[str]) -> bytes:
        if args == ["rev-list", "--all"]:
            return f"{commit}\n".encode()
        if args == ["ls-tree", "-r", "-l", "-z", "--full-tree", commit]:
            entries = [f"100644 blob {object_id} {len(secret)}\t{name}" for name in literal_names]
            entries.extend(
                [
                    f"100644 blob {skipped_internal} {len(secret)}\t.venv/secret.txt",
                    f"100644 blob {skipped_traversal} {len(secret)}\t../secret.txt",
                ]
            )
            return ("\0".join(entries) + "\0").encode()
        if args == ["cat-file", "blob", object_id]:
            return secret.encode()
        raise AssertionError(f"unsafe or unexpected Git call: {args!r}")

    monkeypatch.setattr(secret_scan, "_git", fake_git)

    findings = _history_findings(tmp_path)
    assert findings == [
        Finding(literal_names[0], 1, "aws_access_key", commit),
        Finding(literal_names[1], 1, "aws_access_key", commit),
    ]
    output = render(findings)
    assert all(name in output for name in literal_names)


@pytest.mark.parametrize(
    "suffix",
    [".ps1", ".sh", ".json", ".yaml", ".yml"],
)
def test_release_and_plugin_text_files_are_scanned(tmp_path: Path, suffix: str) -> None:
    repo = tmp_path / "release-scan"
    _init_repo(repo)
    secret = _secret("ghp_", "ReleaseAssetDummyToken012345678901234567")
    path = repo / f"artifact{suffix}"
    path.write_text(secret + "\n", encoding="utf-8")

    assert scan_repository(repo, worktree=True, tracked=False, history=False) == [
        Finding(f"artifact{suffix}", 1, "github_token")
    ]
