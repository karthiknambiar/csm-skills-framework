"""Vendor-neutral connector authoring and ingestion API."""

from csaf.connectors.base import Connector
from csaf.connectors.ingest import ConnectorIngestor
from csaf.connectors.local import CSVConnector, JSONConnector, MarkdownConnector
from csaf.connectors.registry import ConnectorRegistry
from csaf.connectors.types import (
    AuthenticationKind,
    ConnectorCheckpoint,
    ConnectorCredentials,
    ConnectorMetadata,
    ConnectorPage,
    ConnectorRecord,
    IngestionResult,
    NormalizedRecord,
)

__all__ = [
    "AuthenticationKind",
    "CSVConnector",
    "Connector",
    "ConnectorCheckpoint",
    "ConnectorCredentials",
    "ConnectorIngestor",
    "ConnectorMetadata",
    "ConnectorPage",
    "ConnectorRecord",
    "ConnectorRegistry",
    "IngestionResult",
    "JSONConnector",
    "MarkdownConnector",
    "NormalizedRecord",
]
