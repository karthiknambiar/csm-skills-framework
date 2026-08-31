"""Isolated deterministic runtime used by customer-journey simulations."""

import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel

from csaf.core import Runtime, create_runtime
from csaf.office import OfficeCLIError, OfficeRenderRequest
from csaf.schemas import MemoryRecord, MemoryRecordCreate
from csaf.simulations.faults import FaultRegistry
from csaf.skills import Artifact, SkillRunResult

_DATABASE_FILENAME = "simulation.sqlite3"
_DATABASE_RESERVATION_ERROR = "simulation database already exists or is unsafe"
_RUNTIME_INITIALIZATION_ERROR = "simulation runtime initialization failed"
_WORKSPACE_MARKER = "<workspace>"


class _FrozenDict(dict[str, Any]):
    """JSON-object-compatible dictionary that cannot be mutated."""

    def _immutable(self, *_: object, **__: object) -> None:
        raise TypeError("canonical simulation data is immutable")

    def __copy__(self) -> "_FrozenDict":
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> "_FrozenDict":
        memo[id(self)] = self
        return self

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


@dataclass(slots=True)
class MutableClock:
    """A bounded, deterministic clock that never reads wall time."""

    current: datetime

    def __post_init__(self) -> None:
        _require_aware(self.current, "current")

    def now(self) -> datetime:
        """Return the current simulated instant."""

        return self.current

    def advance(self, seconds: int) -> None:
        """Advance by a positive duration of at most one year."""

        if type(seconds) is not int:
            raise TypeError("seconds must be an integer")
        if not 1 <= seconds <= 31_536_000:
            raise ValueError("seconds must be between 1 and 31536000")
        self.current += timedelta(seconds=seconds)


class SimulationOfficeRenderer:
    """Record Office requests and render deterministic in-memory artifacts."""

    def __init__(self, faults: FaultRegistry) -> None:
        self._faults = faults
        self._requests: list[OfficeRenderRequest] = []

    @property
    def requests(self) -> tuple[OfficeRenderRequest, ...]:
        """Return render attempts in call order as immutable request copies."""

        return tuple(self._requests)

    def render(self, request: OfficeRenderRequest) -> bytes:
        """Render one request without invoking OfficeCLI or touching files."""

        recorded = request.model_copy(deep=True)
        self._requests.append(recorded)
        if self._faults.consume("office_missing"):
            raise OfficeCLIError("OfficeCLI executable was not found: officecli")
        if self._faults.consume("office_render_failure"):
            raise OfficeCLIError("simulated office render failure")
        if recorded.template_path is not None and self._faults.consume("corrupt_template"):
            raise OfficeCLIError(f"Office template was not found: {recorded.template_path}")
        return (
            f"simulation:{recorded.format.value}:{recorded.operation.value}:{recorded.title}"
        ).encode()

    def _restore_request_count(self, count: int) -> None:
        del self._requests[count:]


@dataclass(frozen=True, slots=True)
class _WorldCheckpoint:
    memory_ids: frozenset[str]
    artifacts: tuple[tuple[str, bytes], ...]
    office_request_count: int
    id_counter: int


@dataclass(slots=True)
class _DeterministicIdFactory:
    """Generate deterministic UUIDs while exposing checkpointable state."""

    seed: int
    counter: int = 0

    def __call__(self) -> UUID:
        result = uuid5(NAMESPACE_URL, f"{self.seed}:{self.counter}")
        self.counter += 1
        return result


@dataclass(slots=True)
class SimulationWorld:
    """Own every mutable dependency for one deterministic simulation run."""

    workspace: Path
    database_path: Path
    runtime: Runtime
    clock: MutableClock
    faults: FaultRegistry
    office: SimulationOfficeRenderer
    _id_factory: _DeterministicIdFactory
    _closed: bool = False

    @classmethod
    def create(cls, workspace: Path, start: datetime, seed: int) -> "SimulationWorld":
        """Create a runtime isolated beneath an exact resolved workspace."""

        _require_aware(start, "start")
        if type(seed) is not int:
            raise TypeError("seed must be an integer")
        resolved_workspace = Path(workspace).resolve()
        if resolved_workspace.exists() and not resolved_workspace.is_dir():
            raise NotADirectoryError("simulation workspace is not a directory") from None
        resolved_workspace.mkdir(parents=True, exist_ok=True)
        database_path = resolved_workspace / _DATABASE_FILENAME
        database_identity = _reserve_database(database_path, resolved_workspace)
        clock = MutableClock(start)
        faults = FaultRegistry()
        office = SimulationOfficeRenderer(faults)
        id_factory = _DeterministicIdFactory(seed)
        runtime_failed = False
        try:
            runtime = create_runtime(
                database_path,
                office_renderer=office,
                now=clock.now,
                id_factory=id_factory,
            )
        except Exception:
            runtime_failed = True
            try:
                _remove_reserved_database(database_path, database_identity)
            except OSError:
                pass
        if runtime_failed:
            raise ValueError(_RUNTIME_INITIALIZATION_ERROR) from None
        return cls(
            workspace=resolved_workspace,
            database_path=database_path,
            runtime=runtime,
            clock=clock,
            faults=faults,
            office=office,
            _id_factory=id_factory,
        )

    def seed(self, records: Sequence[MemoryRecordCreate]) -> tuple[MemoryRecord, ...]:
        """Append supplied domain records in order and return persisted values."""

        return tuple(self.runtime.memory.append(record) for record in records)

    def memory_snapshot(self) -> tuple[_FrozenDict, ...]:
        """Return every persisted record in stable customer/revision order."""

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM memory_records
                ORDER BY customer_id ASC, COALESCE(logical_key, '') ASC,
                         revision ASC, created_at ASC, id ASC
                """
            ).fetchall()
        return tuple(
            _freeze(
                {
                    "id": row["id"],
                    "customer_id": row["customer_id"],
                    "kind": row["kind"],
                    "content": row["content"],
                    "logical_key": row["logical_key"],
                    "revision": row["revision"],
                    "metadata": json.loads(row["metadata_json"]),
                    "sources": json.loads(row["sources_json"]),
                    "confidence": row["confidence"],
                    "occurred_at": row["occurred_at"],
                    "created_at": row["created_at"],
                }
            )
            for row in rows
        )

    def _checkpoint(self) -> _WorldCheckpoint:
        directory = self.workspace / "artifacts"
        artifacts: list[tuple[str, bytes]] = []
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("simulation artifact directory is unsafe")
            for path in sorted(directory.rglob("*")):
                if path.is_symlink():
                    raise ValueError("simulation artifact directory is unsafe")
                if path.is_file():
                    artifacts.append((path.relative_to(directory).as_posix(), path.read_bytes()))
        return _WorldCheckpoint(
            memory_ids=frozenset(record["id"] for record in self.memory_snapshot()),
            artifacts=tuple(artifacts),
            office_request_count=len(self.office.requests),
            id_counter=self._id_factory.counter,
        )

    def _restore(self, checkpoint: _WorldCheckpoint) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            rows = connection.execute("SELECT id FROM memory_records").fetchall()
            connection.executemany(
                "DELETE FROM memory_records WHERE id = ?",
                ((row[0],) for row in rows if row[0] not in checkpoint.memory_ids),
            )
        directory = self.workspace / "artifacts"
        expected = dict(checkpoint.artifacts)
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("simulation artifact directory is unsafe")
            for path in sorted(directory.rglob("*"), reverse=True):
                if path.is_symlink():
                    raise ValueError("simulation artifact directory is unsafe")
                if path.is_file() and path.relative_to(directory).as_posix() not in expected:
                    path.unlink()
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
        for name, content in checkpoint.artifacts:
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.office._restore_request_count(checkpoint.office_request_count)
        self._id_factory.counter = checkpoint.id_counter

    def canonical_result(self, result: object) -> Any:
        """Freeze a JSON-compatible result while normalizing this workspace path."""

        if isinstance(result, BaseModel):
            value = result.model_dump(mode="json")
        else:
            value = result
        return _canonicalize(value, self.workspace)

    def write_artifacts(
        self,
        result: object,
        directory: Path = Path("artifacts"),
    ) -> tuple[Path, ...]:
        """Write result artifacts under a safe workspace-relative directory."""

        relative_directory = Path(directory)
        if relative_directory.is_absolute():
            raise ValueError("artifact directory must be relative to the workspace")
        if ".." in str(directory).replace("\\", "/").split("/"):
            raise ValueError("relative artifact directory must not contain parent traversal")
        target_directory = (self.workspace / relative_directory).resolve()
        if not _is_beneath(target_directory, self.workspace):
            raise ValueError("artifact directory must remain beneath the workspace") from None

        artifacts = _artifact_sequence(result)
        targets: list[tuple[Path, bytes]] = []
        for artifact in artifacts:
            filename = Path(artifact.filename)
            if (
                filename.is_absolute()
                or len(filename.parts) != 1
                or filename.name != artifact.filename
            ):
                raise ValueError("artifact filename must be a safe basename")
            target = (target_directory / filename).resolve()
            if not _is_beneath(target, self.workspace):
                raise ValueError("artifact filename must remain beneath the workspace") from None
            targets.append((target, bytes(artifact.content)))

        target_directory.mkdir(parents=True, exist_ok=True)
        for target, content in targets:
            target.write_bytes(content)
        return tuple(target for target, _ in targets)

    def close(self) -> None:
        """Release the world-owned database connection once."""

        if self._closed:
            return
        self.runtime.memory.close()
        self._closed = True

    def __enter__(self) -> "SimulationWorld":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def _is_beneath(candidate: Path, workspace: Path) -> bool:
    try:
        candidate.relative_to(workspace)
    except ValueError:
        return False
    return True


def _reserve_database(database_path: Path, workspace: Path) -> tuple[int, int]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            database_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except OSError:
        pass
    if descriptor is None:
        raise ValueError(_DATABASE_RESERVATION_ERROR) from None
    details: os.stat_result | None = None
    inspection_failed = False
    try:
        details = os.fstat(descriptor)
    except OSError:
        inspection_failed = True
    finally:
        try:
            os.close(descriptor)
        except OSError:
            inspection_failed = True
    if inspection_failed or details is None:
        raise ValueError(_DATABASE_RESERVATION_ERROR) from None
    identity = (details.st_dev, details.st_ino)
    contained = False
    if stat.S_ISREG(details.st_mode):
        try:
            contained = database_path.resolve(strict=True).parent == workspace
        except OSError:
            pass
    if not contained:
        try:
            _remove_reserved_database(database_path, identity)
        except OSError:
            pass
        raise ValueError(_DATABASE_RESERVATION_ERROR) from None
    return identity


def _remove_reserved_database(database_path: Path, identity: tuple[int, int]) -> None:
    try:
        details = os.lstat(database_path)
    except FileNotFoundError:
        return
    if stat.S_ISREG(details.st_mode) and (details.st_dev, details.st_ino) == identity:
        database_path.unlink()


def _artifact_sequence(value: object) -> tuple[Artifact, ...]:
    if isinstance(value, SkillRunResult):
        return value.artifacts
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        artifacts = tuple(value)
        if all(isinstance(artifact, Artifact) for artifact in artifacts):
            return artifacts
    raise TypeError("write_artifacts expects a sequence of artifacts or a SkillRunResult")


def _canonicalize(value: object, workspace: Path) -> Any:
    if isinstance(value, BaseModel):
        return _canonicalize(value.model_dump(mode="json"), workspace)
    if isinstance(value, Mapping):
        return _FrozenDict(
            {
                str(key): _canonicalize(item, workspace)
                for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            }
        )
    if isinstance(value, list | tuple):
        return tuple(_canonicalize(item, workspace) for item in value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return _normalize_workspace_path(str(value), workspace)
    if isinstance(value, str):
        return _normalize_workspace_path(value, workspace)
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("canonical simulation floats must be finite")
        return value
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    raise TypeError(f"result contains non-JSON-compatible value: {type(value).__name__}")


def _normalize_workspace_path(value: str, workspace: Path) -> str:
    renderings = {str(workspace), str(workspace).replace("\\", "/")}
    for prefix in sorted(renderings, key=len, reverse=True):
        pattern = re.compile(rf"{re.escape(prefix)}(?=$|[\\/])")
        value = pattern.sub(_WORKSPACE_MARKER, value)
    return value


def _freeze(value: object) -> Any:
    if isinstance(value, Mapping):
        return _FrozenDict({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value
