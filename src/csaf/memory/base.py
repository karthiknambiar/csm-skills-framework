"""Storage contract for Customer Memory implementations."""

from typing import Protocol

from csaf.schemas.memory import MemoryQuery, MemoryRecord, MemoryRecordCreate


class MemoryStore(Protocol):
    """Protocol implemented by replaceable Customer Memory backends."""

    def append(self, record: MemoryRecordCreate) -> MemoryRecord:
        """Append and return a new immutable revision."""

    def get(self, customer_id: str, record_id: str) -> MemoryRecord | None:
        """Return a record only when it belongs to the requested customer."""

    def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Retrieve records using structured and deterministic text filters."""

    def history(self, customer_id: str, logical_key: str) -> list[MemoryRecord]:
        """Return all revisions of a logical record in ascending order."""

    def close(self) -> None:
        """Release resources owned by the store."""
