"""Vetted, packaged QBR template resources."""

import hashlib
import hmac
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from csaf.skills.errors import SkillExecutionError


def _expected_hash(provenance_path: Path, format_name: str, filename: str) -> str:
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if not isinstance(provenance, Mapping):
            raise ValueError("provenance root must be an object")
        templates = provenance.get("templates")
        if not isinstance(templates, Mapping):
            raise ValueError("templates must be an object")
        record = templates.get(format_name)
        if not isinstance(record, Mapping) or record.get("bundled_path") != filename:
            raise ValueError("template provenance record is invalid")
        expected = record.get("bundled_sha256")
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            raise ValueError("template checksum is invalid")
        return expected
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise SkillExecutionError(
            f"bundled QBR template provenance is corrupt: {filename}"
        ) from error


def _validated_template(
    path: Path,
    required_member: str,
    provenance_path: Path,
    format_name: str,
) -> Path:
    if not path.is_file():
        raise SkillExecutionError(f"bundled QBR template is missing: {path.name}")
    expected_hash = _expected_hash(provenance_path, format_name, path.name)
    try:
        content = path.read_bytes()
        actual_hash = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(actual_hash, expected_hash):
            raise SkillExecutionError(f"bundled QBR template is corrupt: {path.name}")
        with ZipFile(path) as archive:
            if archive.testzip() is not None or required_member not in archive.namelist():
                raise SkillExecutionError(f"bundled QBR template is corrupt: {path.name}")
    except (BadZipFile, OSError) as error:
        raise SkillExecutionError(f"bundled QBR template is corrupt: {path.name}") from error
    return path


@contextmanager
def default_qbr_powerpoint() -> Iterator[Path]:
    """Materialize and validate the vetted default PowerPoint template."""

    resources = files(__package__)
    with (
        as_file(resources.joinpath("default-qbr.pptx")) as path,
        as_file(resources.joinpath("provenance.json")) as provenance_path,
    ):
        yield _validated_template(
            path,
            "ppt/presentation.xml",
            provenance_path,
            "powerpoint",
        )


@contextmanager
def default_qbr_word() -> Iterator[Path]:
    """Materialize and validate the vetted default Word template."""

    resources = files(__package__)
    with (
        as_file(resources.joinpath("default-qbr.docx")) as path,
        as_file(resources.joinpath("provenance.json")) as provenance_path,
    ):
        yield _validated_template(path, "word/document.xml", provenance_path, "word")


__all__ = ["default_qbr_powerpoint", "default_qbr_word"]
