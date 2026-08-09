"""Reference connectors for local Markdown, JSON, and CSV files."""

import csv
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from csaf.connectors.errors import ConnectorAuthenticationError, ConnectorDataError
from csaf.connectors.types import (
    AuthenticationKind,
    ConnectorCredentials,
    ConnectorMetadata,
    ConnectorPage,
    ConnectorRecord,
    LocalConnectorConfig,
    NormalizedRecord,
)
from csaf.schemas import MemoryKind


class _LocalConnector(ABC):
    metadata: ConnectorMetadata
    extensions: tuple[str, ...]

    def __init__(self, path: str | Path, default_kind: MemoryKind = MemoryKind.TIMELINE) -> None:
        self._config = LocalConnectorConfig(path=Path(path), default_kind=default_kind)
        self._records: tuple[ConnectorRecord, ...] | None = None

    def authenticate(self, credentials: ConnectorCredentials | None = None) -> None:
        if credentials is not None and credentials.values:
            raise ConnectorAuthenticationError(
                f"{self.metadata.name} does not accept authentication credentials"
            )
        if not self._config.path.exists():
            raise ConnectorDataError(f"source path does not exist: {self._config.path}")

    def fetch_page(self, cursor: str | None = None, limit: int = 100) -> ConnectorPage:
        if limit < 1 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        records = self._load_records()
        try:
            offset = int(cursor or 0)
        except ValueError as error:
            raise ConnectorDataError(f"invalid local connector cursor: {cursor}") from error
        if offset < 0 or offset > len(records):
            raise ConnectorDataError(f"local connector cursor is out of range: {offset}")
        end = min(offset + limit, len(records))
        next_cursor = str(end) if end < len(records) else None
        return ConnectorPage(
            records=records[offset:end],
            next_cursor=next_cursor,
            checkpoint_cursor=str(end),
        )

    def _files(self) -> tuple[Path, ...]:
        path = self._config.path
        if path.is_file():
            if path.suffix.casefold() not in self.extensions:
                raise ConnectorDataError(f"unsupported file extension: {path.suffix}")
            return (path,)
        return tuple(
            sorted(
                file
                for file in path.rglob("*")
                if file.is_file() and file.suffix.casefold() in self.extensions
            )
        )

    def _load_records(self) -> tuple[ConnectorRecord, ...]:
        if self._records is None:
            records: list[ConnectorRecord] = []
            for path in self._files():
                records.extend(self._read_file(path))
            self._records = tuple(records)
        return self._records

    @abstractmethod
    def _read_file(self, path: Path) -> list[ConnectorRecord]:
        """Parse all raw records from one supported file."""

    def normalize(self, record: ConnectorRecord) -> NormalizedRecord:
        payload = record.payload
        try:
            return NormalizedRecord(
                kind=payload.get("kind", self._config.default_kind.value),
                content=payload["content"],
                logical_key=payload.get("logical_key"),
                metadata=payload.get("metadata", {}),
                occurred_at=payload.get("occurred_at"),
                confidence=payload.get("confidence", 1.0),
            )
        except (KeyError, ValidationError) as error:
            raise ConnectorDataError(
                f"invalid record {record.external_id} from {record.source_uri}: {error}"
            ) from error


class MarkdownConnector(_LocalConnector):
    """Treat each Markdown file as one normalized memory record."""

    metadata = ConnectorMetadata(
        name="local-markdown",
        description="Ingest Markdown files from a local path.",
        version="1.0.0",
        authentication=AuthenticationKind.NONE,
        source_types=("markdown",),
        supports_incremental_sync=False,
    )
    extensions = (".md", ".markdown")

    def _read_file(self, path: Path) -> list[ConnectorRecord]:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise ConnectorDataError(f"could not read Markdown file {path}: {error}") from error
        if not content:
            return []
        return [
            ConnectorRecord(
                external_id=str(path.resolve()),
                source_type="markdown",
                source_uri=path.resolve().as_uri(),
                payload={
                    "content": content,
                    "logical_key": f"local-markdown:{path.resolve()}",
                    "metadata": {"filename": path.name},
                },
            )
        ]


class JSONConnector(_LocalConnector):
    """Ingest canonical records from JSON arrays or a ``records`` object key."""

    metadata = ConnectorMetadata(
        name="local-json",
        description="Ingest normalized JSON records from a local path.",
        version="1.0.0",
        authentication=AuthenticationKind.NONE,
        source_types=("json",),
        supports_incremental_sync=False,
    )
    extensions = (".json",)

    def _read_file(self, path: Path) -> list[ConnectorRecord]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ConnectorDataError(f"could not parse JSON file {path}: {error}") from error
        values = document.get("records") if isinstance(document, dict) else document
        if not isinstance(values, list):
            raise ConnectorDataError(f"JSON source must be an array or records object: {path}")
        records: list[ConnectorRecord] = []
        for index, value in enumerate(values, start=1):
            if not isinstance(value, dict):
                raise ConnectorDataError(f"JSON record {index} must be an object: {path}")
            external_id = str(value.get("id") or f"{path.resolve()}#{index}")
            payload = {key: item for key, item in value.items() if key != "id"}
            records.append(
                ConnectorRecord(
                    external_id=external_id,
                    source_type="json",
                    source_uri=path.resolve().as_uri(),
                    payload=payload,
                )
            )
        return records


class CSVConnector(_LocalConnector):
    """Ingest canonical rows from UTF-8 CSV files with a content column."""

    metadata = ConnectorMetadata(
        name="local-csv",
        description="Ingest normalized CSV records from a local path.",
        version="1.0.0",
        authentication=AuthenticationKind.NONE,
        source_types=("csv",),
        supports_incremental_sync=False,
    )
    extensions = (".csv",)
    _reserved = {"id", "content", "kind", "logical_key", "occurred_at", "confidence"}

    def _read_file(self, path: Path) -> list[ConnectorRecord]:
        try:
            with path.open(encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source)
                if reader.fieldnames is None or "content" not in reader.fieldnames:
                    raise ConnectorDataError(f"CSV source requires a content column: {path}")
                rows = list(reader)
        except (OSError, UnicodeError, csv.Error) as error:
            raise ConnectorDataError(f"could not parse CSV file {path}: {error}") from error
        records: list[ConnectorRecord] = []
        for index, row in enumerate(rows, start=2):
            external_id = row.get("id") or f"{path.resolve()}#{index}"
            payload: dict[str, Any] = {
                key: value
                for key, value in row.items()
                if key is not None and key in self._reserved and key != "id" and value
            }
            payload["metadata"] = {
                key: value
                for key, value in row.items()
                if key is not None and key not in self._reserved and value
            }
            records.append(
                ConnectorRecord(
                    external_id=external_id,
                    source_type="csv",
                    source_uri=path.resolve().as_uri(),
                    payload=payload,
                )
            )
        return records
