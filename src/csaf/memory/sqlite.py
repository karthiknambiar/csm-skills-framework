"""SQLite implementation of append-only Customer Memory."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from csaf.core.clock import IdFactory, Now, utc_now
from csaf.schemas.memory import (
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    MemoryRecordCreate,
    SourceReference,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_records (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    logical_key TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    metadata_json TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    occurred_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (customer_id, logical_key, revision)
);
CREATE INDEX IF NOT EXISTS idx_memory_customer_created
    ON memory_records (customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_customer_kind
    ON memory_records (customer_id, kind);
CREATE INDEX IF NOT EXISTS idx_memory_customer_key
    ON memory_records (customer_id, logical_key, revision DESC);
"""


class SQLiteMemoryStore:
    """A small, thread-safe SQLite store that only inserts new revisions."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        now: Now = utc_now,
        id_factory: IdFactory = uuid4,
    ) -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._now = now
        self._id_factory = id_factory
        with self._connection:
            self._connection.executescript(_SCHEMA)

    def append(self, record: MemoryRecordCreate) -> MemoryRecord:
        """Atomically calculate a revision and insert it without modifying history."""

        with self._lock, self._connection:
            revision = self._next_revision(record.customer_id, record.logical_key)
            persisted = MemoryRecord(
                **record.model_dump(),
                id=self._id_factory(),
                revision=revision,
                created_at=self._now(),
            )
            self._connection.execute(
                """
                INSERT INTO memory_records (
                    id, customer_id, kind, content, logical_key, revision,
                    metadata_json, sources_json, confidence, occurred_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(persisted.id),
                    persisted.customer_id,
                    persisted.kind.value,
                    persisted.content,
                    persisted.logical_key,
                    persisted.revision,
                    json.dumps(persisted.metadata, sort_keys=True),
                    json.dumps([source.model_dump(mode="json") for source in persisted.sources]),
                    persisted.confidence,
                    _datetime_to_text(persisted.occurred_at),
                    persisted.created_at.isoformat(),
                ),
            )
        return persisted

    def get(self, customer_id: str, record_id: str) -> MemoryRecord | None:
        """Get a record with mandatory customer scoping."""

        row = self._connection.execute(
            "SELECT * FROM memory_records WHERE customer_id = ? AND id = ?",
            (customer_id, record_id),
        ).fetchone()
        return _row_to_record(row) if row else None

    def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Search metadata and content with portable deterministic SQL filters."""

        clauses = ["customer_id = ?", "confidence >= ?"]
        parameters: list[Any] = [query.customer_id, query.min_confidence]
        if query.kinds:
            placeholders = ", ".join("?" for _ in query.kinds)
            clauses.append(f"kind IN ({placeholders})")
            parameters.extend(kind.value for kind in query.kinds)
        if query.text:
            clauses.append("(content LIKE ? ESCAPE '\\' OR metadata_json LIKE ? ESCAPE '\\')")
            pattern = f"%{_escape_like(query.text)}%"
            parameters.extend((pattern, pattern))
        if query.since:
            clauses.append("COALESCE(occurred_at, created_at) >= ?")
            parameters.append(query.since.isoformat())
        if query.until:
            clauses.append("COALESCE(occurred_at, created_at) <= ?")
            parameters.append(query.until.isoformat())
        if query.latest_only:
            clauses.append(
                "(logical_key IS NULL OR revision = ("
                "SELECT MAX(newer.revision) FROM memory_records AS newer "
                "WHERE newer.customer_id = memory_records.customer_id "
                "AND newer.logical_key = memory_records.logical_key))"
            )
        parameters.append(query.limit)
        rows = self._connection.execute(
            f"SELECT * FROM memory_records WHERE {' AND '.join(clauses)} "  # noqa: S608
            "ORDER BY COALESCE(occurred_at, created_at) DESC, created_at DESC LIMIT ?",
            parameters,
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def history(self, customer_id: str, logical_key: str) -> list[MemoryRecord]:
        """Return immutable revisions for a customer-scoped logical key."""

        rows = self._connection.execute(
            """
            SELECT * FROM memory_records
            WHERE customer_id = ? AND logical_key = ?
            ORDER BY revision ASC
            """,
            (customer_id, logical_key),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def close(self) -> None:
        """Close the SQLite connection."""

        self._connection.close()

    def __enter__(self) -> "SQLiteMemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _next_revision(self, customer_id: str, logical_key: str | None) -> int:
        if logical_key is None:
            return 1
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(revision), 0) + 1 AS revision
            FROM memory_records WHERE customer_id = ? AND logical_key = ?
            """,
            (customer_id, logical_key),
        ).fetchone()
        return int(row["revision"])


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=UUID(row["id"]),
        customer_id=row["customer_id"],
        kind=MemoryKind(row["kind"]),
        content=row["content"],
        logical_key=row["logical_key"],
        revision=row["revision"],
        metadata=json.loads(row["metadata_json"]),
        sources=tuple(
            SourceReference.model_validate(item) for item in json.loads(row["sources_json"])
        ),
        confidence=row["confidence"],
        occurred_at=_text_to_datetime(row["occurred_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _datetime_to_text(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _text_to_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
