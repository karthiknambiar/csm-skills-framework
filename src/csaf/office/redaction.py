"""Redaction for user-visible OfficeCLI diagnostics."""

import re

_CREDENTIAL_URL = re.compile(r"(?i)\bhttps?://[^/\s:@]+:[^@\s/]+@")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|"
    r"client[_-]?secret|token|secret|password))\s*[:=]\s*"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_PEM_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?P<label>(?:[A-Z0-9]+ )?PRIVATE KEY)-----.*?"
    r"-----END (?P=label)-----",
    re.DOTALL,
)
_PEM_PRIVATE_KEY_TRUNCATED = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----.*\Z",
    re.DOTALL,
)
_CREDENTIAL_VALUES = (
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{82}(?![A-Za-z0-9_])"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    re.compile(
        r"(?<![A-Za-z0-9_])(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{24,}"
        r"(?![A-Za-z0-9_])"
    ),
)
_DOUBLE_QUOTED_PATH = re.compile(r'"(?:[a-zA-Z]:[\\/]|\\\\|/)[^"\r\n]*"')
_SINGLE_QUOTED_PATH = re.compile(r"'(?:[a-zA-Z]:[\\/]|\\\\|/)[^'\r\n]*'")
_ABSOLUTE_FILE_PATH = re.compile(
    r"(?i)(?<![\w:/\\])(?:[a-z]:[\\/]|\\\\|/)[^\r\n,;\"']*?"
    r"\.[a-z0-9]{1,12}(?=$|[\s,;:.!?)\]}])"
)
_SIMPLE_WINDOWS_PATH = re.compile(r"(?i)(?<!\w)[a-z]:[\\/][^\s,;:\"'<>|]+")
_SIMPLE_UNC_PATH = re.compile(r"(?<![\\\w])\\\\[^\s,;:\"'<>|]+(?:\\[^\s,;:\"'<>|]+)+")
_SIMPLE_POSIX_PATH = re.compile(r"(?<![:/\w])/(?:[^/\s,;:\"'<>|]+/)*[^/\s,;:\"'<>|]+")


def redact_officecli_message(message: str, *, redact_paths: bool = True) -> str:
    """Remove local paths and credential-shaped values from diagnostic text."""

    redacted = _CREDENTIAL_URL.sub("<redacted-credential>", message)
    redacted = _PEM_PRIVATE_KEY_BLOCK.sub("<redacted-secret>", redacted)
    redacted = _PEM_PRIVATE_KEY_TRUNCATED.sub("<redacted-secret>", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted-secret>", redacted)
    for credential_pattern in _CREDENTIAL_VALUES:
        redacted = credential_pattern.sub("<redacted-secret>", redacted)
    if not redact_paths:
        return redacted
    for path_pattern in (
        _DOUBLE_QUOTED_PATH,
        _SINGLE_QUOTED_PATH,
        _ABSOLUTE_FILE_PATH,
        _SIMPLE_WINDOWS_PATH,
        _SIMPLE_UNC_PATH,
        _SIMPLE_POSIX_PATH,
    ):
        redacted = path_pattern.sub("<redacted-path>", redacted)
    return redacted
