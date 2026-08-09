"""Ingest the bundled canonical JSON export into Customer Memory."""

from csaf.connectors import ConnectorIngestor, JSONConnector
from csaf.core import create_runtime
from csaf.schemas import MemoryQuery


def main() -> None:
    runtime = create_runtime()
    try:
        result = ConnectorIngestor(runtime.memory).ingest(
            JSONConnector("examples/data/acme-memory.json"),
            "acme",
        )
        print(result.model_dump_json(indent=2))
        for record in runtime.memory.search(MemoryQuery(customer_id="acme")):
            print(f"{record.kind.value}: {record.content}")
    finally:
        runtime.memory.close()


if __name__ == "__main__":
    main()
