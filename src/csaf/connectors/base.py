"""Connector protocol implemented by SaaS and local source adapters."""

from typing import Protocol

from csaf.connectors.types import (
    ConnectorCredentials,
    ConnectorMetadata,
    ConnectorPage,
    ConnectorRecord,
    NormalizedRecord,
)


class Connector(Protocol):
    """Contract for authentication, paginated extraction, and normalization."""

    metadata: ConnectorMetadata

    def authenticate(self, credentials: ConnectorCredentials | None = None) -> None:
        """Validate or establish access without leaking credential values."""

    def fetch_page(self, cursor: str | None = None, limit: int = 100) -> ConnectorPage:
        """Fetch one stable page and return an opaque continuation cursor."""

    def normalize(self, record: ConnectorRecord) -> NormalizedRecord:
        """Convert one vendor-specific record into the internal schema."""
