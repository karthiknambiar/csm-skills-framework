"""Load deterministic golden cases from JSON files or directories."""

import json
from pathlib import Path

from pydantic import ValidationError

from csaf.evaluations.types import EvaluationCase


class GoldenDatasetError(ValueError):
    """Raised when a golden dataset cannot be parsed or validated."""


def load_golden_cases(path: str | Path) -> tuple[EvaluationCase, ...]:
    """Load one file or all JSON files in a directory in deterministic order."""

    source = Path(path)
    files = tuple(sorted(source.glob("*.json"))) if source.is_dir() else (source,)
    if not files or any(not file.is_file() for file in files):
        raise GoldenDatasetError(f"golden dataset path was not found: {source}")
    cases: list[EvaluationCase] = []
    for file in files:
        try:
            document = json.loads(file.read_text(encoding="utf-8"))
            values = document if isinstance(document, list) else [document]
            cases.extend(EvaluationCase.model_validate(value) for value in values)
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
            raise GoldenDatasetError(f"invalid golden dataset {file}: {error}") from error
    names = [case.name for case in cases]
    if len(names) != len(set(names)):
        raise GoldenDatasetError("golden case names must be unique")
    return tuple(cases)
