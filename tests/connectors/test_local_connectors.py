"""Conformance and ingestion tests for local reference connectors."""

import json
from pathlib import Path

import pytest

from csaf.connectors import (
    ConnectorCheckpoint,
    ConnectorIngestor,
    ConnectorRegistry,
    CSVConnector,
    JSONConnector,
    MarkdownConnector,
)
from csaf.connectors.errors import ConnectorDataError, DuplicateConnectorError
from csaf.memory import SQLiteMemoryStore
from csaf.schemas import MemoryKind, MemoryQuery


def create_sources(directory: Path) -> dict[str, Path]:
    markdown = directory / "notes.md"
    markdown.write_text("# Customer note\n\nMigration kickoff completed.")
    json_path = directory / "records.json"
    json_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "id": "risk-1",
                        "kind": "risk",
                        "content": "Renewal timeline is compressed.",
                        "logical_key": "risk:renewal-timeline",
                        "occurred_at": "2026-08-01T10:00:00Z",
                        "confidence": 0.8,
                        "metadata": {"owner": "csm"},
                    },
                    {
                        "id": "commitment-1",
                        "kind": "commitment",
                        "content": "Send the migration plan.",
                    },
                ]
            }
        )
    )
    csv_path = directory / "records.csv"
    csv_path.write_text(
        "id,kind,content,logical_key,occurred_at,confidence,owner\n"
        "usage-1,product_usage,Weekly active users grew 12%,usage:wau,"
        "2026-08-02T10:00:00Z,0.9,analytics\n"
    )
    return {"markdown": markdown, "json": json_path, "csv": csv_path}


@pytest.mark.parametrize(
    ("connector_type", "source_name", "expected_kind"),
    [
        (MarkdownConnector, "markdown", MemoryKind.TIMELINE),
        (JSONConnector, "json", MemoryKind.RISK),
        (CSVConnector, "csv", MemoryKind.PRODUCT_USAGE),
    ],
)
def test_local_connector_conformance(
    tmp_path: Path,
    connector_type: type[MarkdownConnector | JSONConnector | CSVConnector],
    source_name: str,
    expected_kind: MemoryKind,
) -> None:
    sources = create_sources(tmp_path)
    connector = connector_type(sources[source_name])

    connector.authenticate()
    page = connector.fetch_page(limit=1)
    normalized = connector.normalize(page.records[0])

    assert connector.metadata.name.startswith("local-")
    assert connector.metadata.authentication.value == "none"
    assert page.checkpoint_cursor == "1"
    assert normalized.kind is expected_kind
    assert normalized.content


def test_json_connector_paginates_and_resumes_from_checkpoint(tmp_path: Path) -> None:
    connector = JSONConnector(create_sources(tmp_path)["json"])
    first = connector.fetch_page(limit=1)
    second = connector.fetch_page(cursor=first.next_cursor, limit=1)

    assert [record.external_id for record in first.records] == ["risk-1"]
    assert first.next_cursor == "1"
    assert [record.external_id for record in second.records] == ["commitment-1"]
    assert second.next_cursor is None
    assert second.checkpoint_cursor == "2"


def test_ingestor_appends_normalized_records_with_provenance_and_checkpoint(
    tmp_path: Path,
) -> None:
    connector = JSONConnector(create_sources(tmp_path)["json"])
    with SQLiteMemoryStore() as memory:
        ingestor = ConnectorIngestor(memory)

        first = ingestor.ingest(connector, "acme", page_size=1, max_pages=1)

        assert first.records_written == 1
        assert first.checkpoint.cursor == "1"
        assert first.checkpoint.state["completed"] is False
        risk = memory.search(
            MemoryQuery(customer_id="acme", kinds=(MemoryKind.RISK,))
        )[0]
        assert risk.sources[0].source_id == "risk-1"
        assert risk.metadata["connector"] == "local-json"
        assert risk.metadata["owner"] == "csm"

        second = ingestor.ingest(
            connector,
            "acme",
            checkpoint=first.checkpoint,
            page_size=1,
        )

        assert second.records_written == 1
        assert second.checkpoint.state["completed"] is True
        assert len(memory.search(MemoryQuery(customer_id="acme"))) == 2

        no_op = ingestor.ingest(connector, "acme", checkpoint=second.checkpoint)
        assert no_op.records_written == 0
        assert len(memory.search(MemoryQuery(customer_id="acme"))) == 2


def test_registry_discovers_connectors_and_rejects_duplicate_names(tmp_path: Path) -> None:
    source = create_sources(tmp_path)["json"]
    registry = ConnectorRegistry()
    registry.register(JSONConnector(source))

    assert registry.names() == ("local-json",)
    assert registry.get("local-json").metadata.source_types == ("json",)

    with pytest.raises(DuplicateConnectorError):
        registry.register(JSONConnector(source))


def test_local_connector_reports_invalid_source_data(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json")
    connector = JSONConnector(invalid)

    with pytest.raises(ConnectorDataError, match="could not parse JSON"):
        connector.fetch_page()


def test_checkpoint_cannot_be_used_with_another_connector(tmp_path: Path) -> None:
    sources = create_sources(tmp_path)
    checkpoint = ConnectorCheckpoint(connector_name="local-csv", cursor="0")
    with SQLiteMemoryStore() as memory:
        with pytest.raises(ValueError, match="different connector"):
            ConnectorIngestor(memory).ingest(
                JSONConnector(sources["json"]),
                "acme",
                checkpoint=checkpoint,
            )


def test_csv_connector_requires_content_column(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.csv"
    invalid.write_text("id,kind\n1,risk\n")

    with pytest.raises(ConnectorDataError, match="requires a content column"):
        CSVConnector(invalid).fetch_page()
