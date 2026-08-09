# Customer Memory model

Customer Memory is the durable, append-only context shared by every skill. A
record belongs to exactly one customer and represents either a new fact/event or
a new revision of an existing logical item.

## Record fields

| Field | Purpose |
| --- | --- |
| `id` | Immutable UUID assigned to this revision |
| `customer_id` | Mandatory tenant/customer boundary |
| `kind` | Normalized category such as `risk`, `meeting`, or `product_usage` |
| `content` | Human-readable fact or event content |
| `logical_key` | Optional identity shared by revisions of one logical item |
| `revision` | Monotonically increasing number within customer and logical key |
| `metadata` | JSON-compatible structured attributes |
| `sources` | Source system, external ID, URI, excerpt, and source timestamp |
| `confidence` | Value from `0.0` to `1.0` describing claim confidence |
| `occurred_at` | When the customer event occurred |
| `created_at` | When CSAF persisted this revision |

Timestamps must include a timezone. Metadata cannot contain arbitrary Python
objects because records must remain portable across SQLite and future PostgreSQL
implementations.

## Normalized kinds

The initial schema includes profile, stakeholder, meeting, support, timeline,
product usage, roadmap, commitment, risk, feature request, QBR, success plan,
renewal, health, and artifact records. Connectors map vendor concepts into these
categories before writing memory.

## Append-only revisions

Appending the same `customer_id` and `logical_key` produces the next revision;
it never updates the previous row:

```python
memory.append(
    MemoryRecordCreate(
        customer_id="acme",
        kind=MemoryKind.RISK,
        logical_key="risk:migration",
        content="Migration is delayed.",
    )
)
memory.append(
    MemoryRecordCreate(
        customer_id="acme",
        kind=MemoryKind.RISK,
        logical_key="risk:migration",
        content="Migration validation is complete.",
    )
)
history = memory.history("acme", "risk:migration")
```

`history` contains revisions 1 and 2. Use `latest_only=True` in `MemoryQuery` when
a caller needs current state rather than the audit history.

## Retrieval and isolation

Every `get`, `search`, and `history` operation requires a customer identifier.
Search supports kinds, literal text, time windows, confidence, current revisions,
and bounded result counts. Text search is deterministic in the SQLite backend;
semantic retrieval can be introduced behind `MemoryStore` without changing skill
contracts.

## Provenance rules

1. Derived claims should include at least one `SourceReference` where source
   material exists.
2. `source_id` must be stable in the originating system.
3. Excerpts should be short enough for review but sufficient to verify the claim.
4. URIs locate sources; they are not substitutes for immutable external IDs.
5. Confidence belongs to the normalized claim, not the source system as a whole.
6. Credentials and secrets must never enter content, metadata, or source fields.

## Storage lifecycle

`SQLiteMemoryStore` is the development implementation and supports in-memory or
file-backed databases. Call `close()` or use it as a context manager. Production
backends must satisfy the same `MemoryStore` protocol and preserve customer
isolation, revision order, provenance, and append-only behavior.
