"""Load and validate deterministic simulation scenario datasets."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import ValidationError

from csaf.simulations.schema import IngestFixtureStep, SimulationScenario

_SAFE_VALIDATION_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "title",
        "seed",
        "customers",
        "steps",
        "expectations",
        "type",
        "records",
        "skill",
        "input",
        "expect_error",
        "seconds",
        "fault",
        "remaining_calls",
        "customer_id",
        "fixture",
        "step_id",
        "path",
        "value",
        "term",
        "count",
        "logical_key",
        "revision",
        "values",
    }
)


class SimulationDatasetError(ValueError):
    """A simulation dataset could not be loaded safely and completely."""


class _DuplicateJsonKeyError(ValueError):
    """A JSON object contains a repeated key."""


def _dataset_error(source: Path, message: str, cause: Exception) -> SimulationDatasetError:
    """Build a consistently sourced public dataset error."""

    return SimulationDatasetError(f"{source}: {message}: {cause}")


def _discover_sources(path: Path) -> tuple[Path, ...]:
    """Resolve one JSON file or a deterministic non-recursive directory listing."""

    if path.is_file():
        if path.suffix != ".json":
            cause = ValueError("input file must have a .json suffix")
            raise _dataset_error(path, "unsupported dataset input", cause) from cause
        return (path,)

    if path.is_dir():
        try:
            sources = tuple(
                sorted(
                    (
                        candidate
                        for candidate in path.glob("*.json")
                        if candidate.is_file() and candidate.suffix == ".json"
                    ),
                    key=str,
                )
            )
        except OSError as cause:
            raise _dataset_error(path, "unable to discover scenario files", cause) from cause
        if not sources:
            cause = ValueError("no JSON scenario files found")
            raise _dataset_error(path, "empty dataset directory", cause) from cause
        return sources

    cause = FileNotFoundError("path does not exist") if not path.exists() else ValueError(
        "path is neither a JSON file nor a directory"
    )
    raise _dataset_error(path, "unsupported dataset input", cause) from cause


def _sanitize_validation_location_component(component: object) -> str:
    """Render only known schema fields and numeric collection indices."""

    if type(component) is int:
        return str(component)
    if isinstance(component, str) and component in _SAFE_VALIDATION_FIELDS:
        return component
    return "<field>"


def _validation_summary(error: ValidationError) -> str:
    """Summarize validation failures without echoing source payload values."""

    details: list[str] = []
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join(
            _sanitize_validation_location_component(part)
            for part in item["loc"]
        ) or "scenario"
        details.append(f"{location}: {item['type']}")
    return "; ".join(details)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting repeated keys at every depth."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _validate_fixture_boundaries(scenario: SimulationScenario) -> None:
    """Reject fixture references that can escape the fixture dataset root."""

    for step in scenario.steps:
        if not isinstance(step, IngestFixtureStep):
            continue
        fixture = step.fixture
        components = fixture.replace("\\", "/").split("/")
        windows_path = PureWindowsPath(fixture)
        if (
            PurePosixPath(fixture).is_absolute()
            or bool(windows_path.drive)
            or bool(windows_path.root)
            or ".." in components
        ):
            raise ValueError(f"fixture path escapes the dataset boundary: {fixture}")


def _load_source(source: Path) -> SimulationScenario:
    """Load and validate exactly one scenario source file."""

    try:
        raw = source.read_bytes()
    except OSError as cause:
        raise _dataset_error(source, "unable to read scenario file", cause) from cause

    decode_failure: ValueError | None = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        decode_failure = ValueError(f"invalid UTF-8 at byte offset {error.start}")
    if decode_failure is not None:
        raise _dataset_error(
            source, "scenario file is not UTF-8", decode_failure
        ) from decode_failure

    json_failure: ValueError | None = None
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as error:
        detail = f"malformed JSON at line {error.lineno} column {error.colno}"
        json_failure = ValueError(detail)
    except _DuplicateJsonKeyError:
        json_failure = ValueError("duplicate JSON object key")
    except RecursionError:
        json_failure = ValueError("JSON nesting exceeds supported depth")
    except ValueError:
        json_failure = ValueError("JSON value could not be decoded")
    if json_failure is not None:
        raise _dataset_error(source, "invalid JSON document", json_failure) from json_failure

    if not isinstance(payload, dict):
        cause = TypeError("scenario root must be a JSON object")
        raise _dataset_error(source, "invalid scenario root", cause) from cause

    validation_failure: ValueError | None = None
    try:
        scenario = SimulationScenario.model_validate(payload)
    except ValidationError as error:
        validation_failure = ValueError(_validation_summary(error))
    if validation_failure is not None:
        raise _dataset_error(
            source, "scenario schema validation failed", validation_failure
        ) from validation_failure

    try:
        _validate_fixture_boundaries(scenario)
    except ValueError as cause:
        raise _dataset_error(source, "invalid fixture path", cause) from cause
    return scenario


def load_scenarios(path: str | Path) -> tuple[SimulationScenario, ...]:
    """Load all scenarios from one JSON file or a flat JSON directory."""

    source_path = Path(path)
    sources = _discover_sources(source_path)
    scenarios: list[SimulationScenario] = []
    scenario_sources: dict[str, Path] = {}
    for source in sources:
        scenario = _load_source(source)
        if scenario.id in scenario_sources:
            first_source = scenario_sources[scenario.id]
            cause = ValueError(
                f"duplicate scenario id {scenario.id!r}; first defined in {first_source}"
            )
            raise _dataset_error(source, "duplicate scenario id", cause) from cause
        scenario_sources[scenario.id] = source
        scenarios.append(scenario)
    return tuple(scenarios)


__all__ = ["SimulationDatasetError", "load_scenarios"]
