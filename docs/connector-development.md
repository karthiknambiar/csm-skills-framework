# Connector development guide

Connectors keep vendor-specific extraction at the edge of CSAF. They emit raw
pages, normalize each record into the internal schema, and let
`ConnectorIngestor` append customer-scoped memory with provenance.

## Lifecycle

```text
discover -> authenticate -> fetch page -> normalize -> append memory -> checkpoint
```

Every connector implements `Connector` and declares `ConnectorMetadata` with a
stable kebab-case name, semantic version, authentication kind, source types, and
incremental-sync capability.

## Contract

```python
from csaf.connectors import (
    ConnectorMetadata,
    ConnectorPage,
    ConnectorRecord,
    NormalizedRecord,
)


class ExampleConnector:
    metadata = ConnectorMetadata(
        name="example-crm",
        description="Read customer notes from Example CRM.",
        version="1.0.0",
        authentication="oauth2",
        source_types=("crm-note",),
    )

    def authenticate(self, credentials=None): ...

    def fetch_page(self, cursor=None, limit=100) -> ConnectorPage: ...

    def normalize(self, record: ConnectorRecord) -> NormalizedRecord: ...
```

Credential values use Pydantic `SecretStr` and must never be copied into raw
payloads, logs, checkpoints, normalized metadata, or source references.

## Pagination and checkpoints

- Cursors are opaque to `ConnectorIngestor`; only the connector interprets them.
- A page returns `next_cursor` for another request and `checkpoint_cursor` for
  durable resume state.
- Checkpoints are bound to a connector name and rejected by other connectors.
- `max_pages` allows bounded jobs to stop and resume safely.
- Connector checkpoints must not contain credentials.

## Normalization

Normalized records declare a memory kind, content, optional logical key,
JSON-compatible metadata, timestamp, and confidence. The ingestor adds connector
identity and external record ID, creates a source reference, scopes the record to
the requested customer, and uses append-only memory writes.

## Local reference connectors

The bundled Markdown, JSON, and CSV implementations serve as executable examples:

```bash
csaf connector ingest markdown notes/ --customer-id acme
csaf connector ingest json export.json --customer-id acme \
  --checkpoint-file .checkpoints/acme-json.json
csaf connector ingest csv export.csv --customer-id acme
```

JSON accepts an array or `{ "records": [...] }`. JSON objects may contain `id`,
`kind`, `content`, `logical_key`, `metadata`, `occurred_at`, and `confidence`.
CSV uses the same names as columns; additional columns become metadata. Markdown
treats each non-empty file as one record and uses `--default-kind`.

New community connectors should run the same conformance expectations demonstrated
in `tests/connectors/test_local_connectors.py`: discovery metadata, authentication,
stable pagination, normalization, provenance, checkpoint resume, customer
isolation, and malformed-source errors.
