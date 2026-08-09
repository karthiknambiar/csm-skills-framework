"""Contract tests for the SQLite Customer Memory backend."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from csaf.memory import SQLiteMemoryStore
from csaf.schemas import MemoryKind, MemoryQuery, MemoryRecordCreate, SourceReference


@pytest.fixture
def store() -> SQLiteMemoryStore:
    memory = SQLiteMemoryStore()
    yield memory
    memory.close()


def record(customer_id: str = "acme", **overrides: object) -> MemoryRecordCreate:
    values: dict[str, object] = {
        "customer_id": customer_id,
        "kind": MemoryKind.RISK,
        "content": "Adoption is blocked by an incomplete migration.",
        "logical_key": "risk:migration",
        "metadata": {"owner": "customer"},
        "sources": (
            SourceReference(
                source_type="meeting",
                source_id="meeting-42",
                excerpt="The migration is still incomplete.",
            ),
        ),
        "confidence": 0.8,
        "occurred_at": datetime(2026, 8, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return MemoryRecordCreate.model_validate(values)


def test_append_preserves_provenance_and_assigns_revision(
    store: SQLiteMemoryStore,
) -> None:
    saved = store.append(record())

    assert saved.revision == 1
    assert saved.sources[0].source_id == "meeting-42"
    assert saved.metadata == {"owner": "customer"}
    assert store.get("acme", str(saved.id)) == saved


def test_append_creates_history_instead_of_overwriting(store: SQLiteMemoryStore) -> None:
    first = store.append(record(content="Migration is incomplete."))
    second = store.append(record(content="Migration completed."))

    history = store.history("acme", "risk:migration")

    assert [item.revision for item in history] == [1, 2]
    assert [item.content for item in history] == [first.content, second.content]
    assert store.search(MemoryQuery(customer_id="acme", latest_only=True)) == [second]


def test_records_are_isolated_by_customer(store: SQLiteMemoryStore) -> None:
    acme = store.append(record("acme"))
    other = store.append(record("globex"))

    assert store.get("globex", str(acme.id)) is None
    assert store.search(MemoryQuery(customer_id="acme")) == [acme]
    assert store.search(MemoryQuery(customer_id="globex")) == [other]
    assert store.history("globex", "risk:migration") == [other]


def test_search_filters_kind_text_time_and_confidence(store: SQLiteMemoryStore) -> None:
    match = store.append(record())
    store.append(
        record(
            kind=MemoryKind.COMMITMENT,
            content="Schedule enablement training.",
            logical_key="commitment:training",
            confidence=0.4,
        )
    )

    result = store.search(
        MemoryQuery(
            customer_id="acme",
            kinds=(MemoryKind.RISK,),
            text="migration",
            since=datetime(2026, 7, 1, tzinfo=UTC),
            until=datetime(2026, 8, 2, tzinfo=UTC),
            min_confidence=0.75,
        )
    )

    assert result == [match]


@pytest.mark.parametrize(
    ("field", "value"),
    [("customer_id", " "), ("content", ""), ("confidence", 1.1)],
)
def test_record_validation_rejects_invalid_input(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        record(**{field: value})


def test_query_rejects_inverted_or_naive_time_windows() -> None:
    with pytest.raises(ValidationError):
        MemoryQuery(
            customer_id="acme",
            since=datetime(2026, 8, 2, tzinfo=UTC),
            until=datetime(2026, 8, 1, tzinfo=UTC),
        )

    with pytest.raises(ValidationError):
        MemoryQuery(customer_id="acme", since=datetime(2026, 8, 1))
