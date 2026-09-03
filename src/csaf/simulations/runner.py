"""Deterministic scenario execution with failure-preserving evidence."""

import base64
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from csaf.connectors import ConnectorIngestor
from csaf.connectors.errors import ConnectorDataError, ConnectorError
from csaf.connectors.types import (
    AuthenticationKind,
    ConnectorCredentials,
    ConnectorMetadata,
    ConnectorPage,
    ConnectorRecord,
    NormalizedRecord,
)
from csaf.simulations.schema import (
    AdvanceTimeStep,
    ClearFaultsStep,
    IngestFixtureStep,
    RunSkillStep,
    SeedMemoryStep,
    SetFaultStep,
    SimulationRun,
    SimulationScenario,
    SimulationSnapshot,
    StepResult,
)
from csaf.simulations.world import SimulationWorld
from csaf.skills.errors import SkillError, SkillExecutionError
from csaf.skills.types import Artifact

_FIXTURE_FIELDS = frozenset(
    {"id", "kind", "content", "logical_key", "metadata", "occurred_at", "confidence"}
)
_SECRET_PATTERN = re.compile(r"\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_MATCHABLE_ERROR_MESSAGES = frozenset(
    {
        "QBR artifact rendering failed",
        "simulated connector rate limit",
        "simulated connector timeout",
    }
)


class _FixtureConnector:
    """A prevalidated, local-only JSON fixture connector with deterministic pages."""

    metadata = ConnectorMetadata(
        name="simulation-fixture",
        description="Ingest prevalidated JSON simulation fixture records.",
        version="1.0.0",
        authentication=AuthenticationKind.NONE,
        source_types=("json",),
        supports_incremental_sync=False,
    )

    def __init__(
        self,
        records: tuple[tuple[ConnectorRecord, NormalizedRecord], ...],
        world: SimulationWorld,
    ) -> None:
        self._records = records
        self._normalized = {source.external_id: normalized for source, normalized in records}
        if len(self._normalized) != len(records):
            raise ConnectorDataError("fixture record ids must be unique")
        self._world = world

    def authenticate(self, credentials: ConnectorCredentials | None = None) -> None:
        """Consume transient connector faults before the ingestor can append data."""

        if credentials is not None and credentials.values:
            raise ConnectorDataError("simulation fixtures do not accept credentials")
        if self._world.faults.consume("connector_timeout"):
            raise ConnectorError("simulated connector timeout")
        if self._world.faults.consume("connector_rate_limit"):
            raise ConnectorError("simulated connector rate limit")

    def fetch_page(self, cursor: str | None = None, limit: int = 100) -> ConnectorPage:
        """Return an immutable, stable slice without external access."""

        if limit < 1 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        try:
            offset = int(cursor or "0")
        except ValueError as error:
            raise ConnectorDataError("invalid fixture connector cursor") from error
        if offset < 0 or offset > len(self._records):
            raise ConnectorDataError("fixture connector cursor is out of range")
        end = min(offset + limit, len(self._records))
        next_cursor = str(end) if end < len(self._records) else None
        return ConnectorPage(
            records=tuple(record for record, _ in self._records[offset:end]),
            next_cursor=next_cursor,
            checkpoint_cursor=str(end),
        )

    def normalize(self, record: ConnectorRecord) -> NormalizedRecord:
        """Return the fixture record's schema-validated normalized representation."""

        try:
            return self._normalized[record.external_id]
        except KeyError as error:
            raise ConnectorDataError("fixture connector record was not found") from error


class JourneyRunner:
    """Execute one scenario without taking ownership of the supplied simulation world."""

    def __init__(self, world: SimulationWorld, fixture_root: Path | None = None) -> None:
        self._world = world
        self._fixture_root = Path(fixture_root) if fixture_root is not None else world.workspace

    def run(self, scenario: SimulationScenario) -> SimulationRun:
        """Run every step in order, retaining a before/after boundary for each attempt."""

        started_at = self._world.clock.now()
        steps: list[StepResult] = []
        outputs: list[object] = []
        serialized_outputs: list[str] = []
        updates: list[dict[str, object]] = []
        artifacts: list[dict[str, object]] = []
        succeeded = True
        initial_snapshot = self._snapshot()

        for index, step in enumerate(scenario.steps, start=1):
            step_id = step.id or f"step-{index}"
            before = initial_snapshot if index == 1 else self._snapshot()
            checkpoint = self._world._checkpoint()
            try:
                output, step_updates, step_artifacts = self._dispatch(step)
            except Exception as error:
                after = self._snapshot()
                error_type, error_message = self._error_details(error)
                matched = self._matches_expected_error(step, error_type, error_message)
                steps.append(
                    StepResult(
                        id=step_id,
                        type=step.type,
                        success=matched,
                        started_at=before.captured_at,
                        completed_at=after.captured_at,
                        before=before,
                        after=after,
                        error=error_message,
                        error_type=error_type,
                        error_message=error_message,
                        expected_error=matched,
                    )
                )
                if not matched:
                    succeeded = False
                    break
                continue

            if isinstance(step, RunSkillStep | IngestFixtureStep) and step.expect_error is not None:
                self._world._restore(checkpoint)
                after = self._snapshot()
                error_message = "expected error was not raised"
                steps.append(
                    StepResult(
                        id=step_id,
                        type=step.type,
                        success=False,
                        started_at=before.captured_at,
                        completed_at=after.captured_at,
                        before=before,
                        after=after,
                        output=output,
                        updates=step_updates,
                        artifacts=step_artifacts,
                        error=error_message,
                        error_type="ExpectedErrorNotRaised",
                        error_message=error_message,
                    )
                )
                succeeded = False
                break

            after = self._snapshot()
            steps.append(
                StepResult(
                    id=step_id,
                    type=step.type,
                    success=True,
                    started_at=before.captured_at,
                    completed_at=after.captured_at,
                    before=before,
                    after=after,
                    output=output,
                    updates=step_updates,
                    artifacts=step_artifacts,
                )
            )
            updates.extend(step_updates)
            artifacts.extend(step_artifacts)
            if isinstance(step, RunSkillStep):
                outputs.append(output)
                serialized_outputs.append(_canonical_json(output))

        final_snapshot = self._snapshot()
        return SimulationRun(
            scenario_id=scenario.id,
            seed=scenario.seed,
            started_at=started_at,
            completed_at=self._world.clock.now(),
            success=succeeded,
            steps=tuple(steps),
            initial_snapshot=initial_snapshot,
            final_snapshot=final_snapshot,
            outputs=tuple(outputs),
            serialized_outputs=tuple(serialized_outputs),
            last_output=outputs[-1] if outputs else None,
            updates=tuple(updates),
            artifacts=tuple(artifacts),
        )

    def _dispatch(
        self, step: object
    ) -> tuple[object | None, tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
        if isinstance(step, SeedMemoryStep):
            persisted = self._world.seed(step.records)
            return None, self._canonical_records(persisted), ()
        if isinstance(step, RunSkillStep):
            result = self._world.runtime.runner.run(
                step.skill,
                step.input,
                artifact_handler=self._write_artifacts,
            )
            return (
                _json_value(self._world.canonical_result(result.output)),
                self._canonical_records(result.memory_updates),
                self._canonical_artifacts(result.artifacts),
            )
        if isinstance(step, AdvanceTimeStep):
            self._world.clock.advance(step.seconds)
            return None, (), ()
        if isinstance(step, SetFaultStep):
            self._world.faults.set(step.fault, step.remaining_calls)
            return None, (), ()
        if isinstance(step, ClearFaultsStep):
            self._world.faults.clear()
            return None, (), ()
        if isinstance(step, IngestFixtureStep):
            memory_before = {record["id"] for record in self._world.memory_snapshot()}
            result = ConnectorIngestor(self._world.runtime.memory).ingest(
                self._fixture_connector(step.fixture), step.customer_id
            )
            updates = tuple(
                _json_value(record)
                for record in self._world.memory_snapshot()
                if record["id"] not in memory_before
            )
            return (
                _json_value(
                    self._world.canonical_result(
                        {
                            "connector_name": result.connector_name,
                            "customer_id": result.customer_id,
                            "records_seen": result.records_seen,
                            "records_written": result.records_written,
                            "memory_record_ids": result.memory_record_ids,
                            "checkpoint": {
                                "cursor": result.checkpoint.cursor,
                                "state": result.checkpoint.state,
                            },
                        }
                    )
                ),
                updates,
                (),
            )
        raise TypeError("unsupported simulation step")

    def _write_artifacts(self, artifacts: tuple[Artifact, ...]) -> None:
        if self._world.faults.consume("artifact_commit_failure"):
            raise RuntimeError("simulated artifact commit failure")
        self._world.write_artifacts(artifacts)

    def _fixture_connector(self, fixture: str) -> _FixtureConnector:
        path = self._safe_fixture_path(fixture)
        return _FixtureConnector(self._load_fixture(path, fixture), self._world)

    def _safe_fixture_path(self, fixture: str) -> Path:
        root = self._fixture_root
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as error:
            raise ConnectorDataError("fixture root is unavailable") from error
        candidate = Path(fixture)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ConnectorDataError("fixture path must be a relative basename")
        path = root / candidate
        if root.is_symlink() or any(
            (root / Path(*candidate.parts[:index])).is_symlink()
            for index in range(1, len(candidate.parts) + 1)
        ):
            raise ConnectorDataError("fixture path must not use symlinks")
        try:
            resolved_path = path.resolve(strict=True)
        except OSError as error:
            raise ConnectorDataError("fixture source is unavailable") from error
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            raise ConnectorDataError("fixture path escapes the fixture root") from None
        if not resolved_path.is_file():
            raise ConnectorDataError("fixture source is not a file")
        return resolved_path

    def _load_fixture(
        self, path: Path, fixture: str
    ) -> tuple[tuple[ConnectorRecord, NormalizedRecord], ...]:
        if path.suffix.casefold() != ".json":
            raise ConnectorDataError("simulation fixtures must be JSON files")
        try:
            document = json.loads(
                _read_fixture_bytes(path).decode("utf-8"),
                object_pairs_hook=_no_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise ConnectorDataError("fixture JSON is invalid") from error
        if not isinstance(document, dict) or set(document) != {"records"}:
            raise ConnectorDataError("fixture must be an object with only a records array")
        records = document["records"]
        if not isinstance(records, list):
            raise ConnectorDataError("fixture records must be an array")

        parsed: list[tuple[ConnectorRecord, NormalizedRecord]] = []
        seen_ids: set[str] = set()
        for index, value in enumerate(records, start=1):
            if not isinstance(value, dict) or set(value) - _FIXTURE_FIELDS:
                raise ConnectorDataError("fixture record has an invalid schema")
            external_id = value.get("id")
            if type(external_id) is not str or not external_id.strip() or external_id in seen_ids:
                raise ConnectorDataError("fixture record ids must be unique non-blank strings")
            seen_ids.add(external_id)
            payload = {key: item for key, item in value.items() if key != "id"}
            try:
                normalized = NormalizedRecord.model_validate(payload)
                source = ConnectorRecord(
                    external_id=external_id,
                    source_type="simulation_fixture",
                    source_uri=f"fixture:{fixture}#{index}",
                    payload=payload,
                )
            except ValidationError as error:
                raise ConnectorDataError("fixture record has an invalid schema") from error
            parsed.append((source, normalized))
        return tuple(parsed)

    def _snapshot(self) -> SimulationSnapshot:
        artifacts = self._artifact_snapshot()
        office_requests = tuple(
            _json_value(self._world.canonical_result(request))
            for request in self._world.office.requests
        )
        return SimulationSnapshot(
            captured_at=self._world.clock.now(),
            memory=tuple(_json_value(record) for record in self._world.memory_snapshot()),
            artifacts=artifacts,
            office_requests=office_requests,
        )

    def _artifact_snapshot(self) -> tuple[dict[str, object], ...]:
        directory = self._world.workspace / "artifacts"
        if not directory.exists():
            return ()
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("simulation artifact directory is unsafe")
        artifacts: list[dict[str, object]] = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            artifacts.append(
                {
                    "filename": path.relative_to(directory).as_posix(),
                    "content": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
            )
        return tuple(artifacts)

    def _canonical_records(self, records: object) -> tuple[dict[str, object], ...]:
        return tuple(_json_value(self._world.canonical_result(record)) for record in records)  # type: ignore[arg-type]

    def _canonical_artifacts(
        self, artifacts: tuple[Artifact, ...]
    ) -> tuple[dict[str, object], ...]:
        canonical: list[dict[str, object]] = []
        for artifact in artifacts:
            value = _json_value(self._world.canonical_result(artifact))
            if not isinstance(value, dict):  # pragma: no cover
                raise TypeError("canonical artifact must be an object")
            canonical.append(
                {
                    **value,
                    "sha256": hashlib.sha256(artifact.content).hexdigest(),
                }
            )
        return tuple(canonical)

    @staticmethod
    def _matches_expected_error(step: object, error_type: str, error_message: str) -> bool:
        expected = step.expect_error if isinstance(step, RunSkillStep | IngestFixtureStep) else None
        return expected is not None and (
            expected == error_type
            or (error_message in _MATCHABLE_ERROR_MESSAGES and expected == error_message)
        )

    def _error_details(self, error: Exception) -> tuple[str, str]:
        if isinstance(error, ValidationError):
            return type(error).__name__, _validation_error_summary(error)
        if isinstance(error, ConnectorError):
            message = _sanitize_error(str(error), self._world.workspace, self._fixture_root)
            if message in {"simulated connector timeout", "simulated connector rate limit"}:
                return type(error).__name__, message
            return type(error).__name__, "connector operation failed"
        if isinstance(error, SkillExecutionError) and str(error).startswith(
            "QBR artifact rendering failed"
        ):
            return type(error).__name__, "QBR artifact rendering failed"
        if isinstance(error, SkillError):
            return type(error).__name__, "skill operation failed"
        return type(error).__name__, "operation failed"


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys that normal ``json.loads`` would silently overwrite."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _read_fixture_bytes(path: Path) -> bytes:
    """Read one regular fixture through a verified descriptor, never a second path open."""

    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ConnectorDataError("fixture source is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except ConnectorDataError:
        raise
    except OSError as error:
        raise ConnectorDataError("fixture source is unavailable") from error
    try:
        opened = os.fstat(descriptor)
        after = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _same_file_identity(before, opened)
            or not _same_file_identity(opened, after)
        ):
            raise ConnectorDataError("fixture source identity changed during read")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            return source.read()
    except ConnectorDataError:
        raise
    except OSError as error:
        raise ConnectorDataError("fixture source is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    """Compare the portable device/inode identity supplied by the host filesystem."""

    first_identity = (getattr(first, "st_dev", None), getattr(first, "st_ino", None))
    second_identity = (getattr(second, "st_dev", None), getattr(second, "st_ino", None))
    if None in first_identity or None in second_identity:
        return False
    return first_identity == second_identity


def _reject_non_finite(value: str) -> object:
    """Reject JSON constants such as NaN and Infinity."""

    raise ValueError(f"non-finite JSON value: {value}")


def _canonical_json(value: object) -> str:
    """Serialize a frozen JSON value in a stable form for downstream graders."""

    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    )


def _validation_error_summary(error: ValidationError) -> str:
    """Return only stable validation categories, never rejected values or contexts."""

    error_types = sorted(
        {
            str(item["type"])
            for item in error.errors(include_url=False, include_context=False, include_input=False)
        }
    )
    return f"validation failed: {', '.join(error_types) or 'invalid input'}"


def _json_value(value: object) -> object:
    """Turn the world's immutable canonical containers into JSON validation inputs."""

    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    return value


def _sanitize_error(error: str, workspace: Path, fixture_root: Path) -> str:
    """Keep stable diagnostics while removing paths, secrets, and traceback fragments."""

    sanitized = error.replace("\r", " ").replace("\n", " ")
    for path in {str(workspace), str(fixture_root), str(workspace).replace("\\", "/")}:
        if path:
            sanitized = sanitized.replace(path, "<path>")
    sanitized = _SECRET_PATTERN.sub("<redacted>", sanitized)
    sanitized = sanitized.replace("Traceback", "error")
    return sanitized[:500]
