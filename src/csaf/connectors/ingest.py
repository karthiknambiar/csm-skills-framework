"""Connector-to-memory ingestion lifecycle."""

from csaf.connectors.base import Connector
from csaf.connectors.errors import ConnectorDataError
from csaf.connectors.types import (
    ConnectorCheckpoint,
    ConnectorCredentials,
    IngestionResult,
)
from csaf.memory import MemoryStore
from csaf.schemas import MemoryRecordCreate, SourceReference


class ConnectorIngestor:
    """Authenticate, page, normalize, append, and return a resumable checkpoint."""

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    def ingest(
        self,
        connector: Connector,
        customer_id: str,
        *,
        credentials: ConnectorCredentials | None = None,
        checkpoint: ConnectorCheckpoint | None = None,
        page_size: int = 100,
        max_pages: int | None = None,
    ) -> IngestionResult:
        if not customer_id.strip():
            raise ValueError("customer_id must not be blank")
        if checkpoint is not None and checkpoint.connector_name != connector.metadata.name:
            raise ValueError("checkpoint belongs to a different connector")
        if page_size < 1 or page_size > 1_000:
            raise ValueError("page_size must be between 1 and 1000")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be positive")
        connector.authenticate(credentials)
        cursor = checkpoint.cursor if checkpoint else None
        if checkpoint is not None and checkpoint.state.get("completed") is True:
            return IngestionResult(
                connector_name=connector.metadata.name,
                customer_id=customer_id,
                records_seen=0,
                records_written=0,
                checkpoint=checkpoint,
                memory_record_ids=(),
            )
        seen = 0
        written_ids: list[str] = []
        pages = 0
        checkpoint_cursor = cursor
        completed = False
        visited_cursors = {cursor}
        while True:
            page = connector.fetch_page(cursor=cursor, limit=page_size)
            pages += 1
            for raw in page.records:
                normalized = connector.normalize(raw)
                persisted = self._memory.append(
                    MemoryRecordCreate(
                        customer_id=customer_id,
                        kind=normalized.kind,
                        content=normalized.content,
                        logical_key=normalized.logical_key,
                        metadata={
                            **normalized.metadata,
                            "connector": connector.metadata.name,
                            "external_id": raw.external_id,
                        },
                        sources=(
                            SourceReference(
                                source_type=raw.source_type,
                                source_id=raw.external_id,
                                uri=raw.source_uri,
                                occurred_at=normalized.occurred_at,
                            ),
                        ),
                        confidence=normalized.confidence,
                        occurred_at=normalized.occurred_at,
                    )
                )
                seen += 1
                written_ids.append(str(persisted.id))
            checkpoint_cursor = page.checkpoint_cursor or page.next_cursor or checkpoint_cursor
            cursor = page.next_cursor
            completed = cursor is None
            if cursor is not None and cursor in visited_cursors:
                raise ConnectorDataError(
                    f"connector returned a repeated pagination cursor: {cursor}"
                )
            visited_cursors.add(cursor)
            if completed or (max_pages is not None and pages >= max_pages):
                break
        final_checkpoint = ConnectorCheckpoint(
            connector_name=connector.metadata.name,
            cursor=checkpoint_cursor,
            state={
                "pages_processed": pages,
                "records_seen": seen,
                "completed": completed,
            },
        )
        return IngestionResult(
            connector_name=connector.metadata.name,
            customer_id=customer_id,
            records_seen=seen,
            records_written=len(written_ids),
            checkpoint=final_checkpoint,
            memory_record_ids=tuple(written_ids),
        )
