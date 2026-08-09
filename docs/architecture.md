# Architecture

## Context

CSAF turns heterogeneous customer signals into durable Customer Memory and
exposes focused skills that use that memory. Transports (CLI and REST), external
systems, model providers, and document formats sit at the edges of the system.
The domain must remain usable without any one of them.

## Architectural boundaries

| Package | Responsibility | May depend on |
| --- | --- | --- |
| `core` | Shared protocols, errors, runtime configuration | Python standard library |
| `schemas` | Stable data contracts crossing package boundaries | `core`, Pydantic |
| `memory` | Append-only persistence, retrieval, provenance, history | `core`, `schemas` |
| `skills` | Skill SDK, registry, execution lifecycle, built-in skills | `core`, `schemas`, `memory`, `prompts`, `office` protocols |
| `workflows` | Composition and orchestration of skills | `core`, `schemas`, `skills` |
| `connectors` | Map external vendor records into internal schemas | `core`, `schemas`, `memory` protocols |
| `office` | Artifact contracts and OfficeCLI adapters | `core`, `schemas`, `templates` |
| `prompts` | Versioned prompt files and loading | `core` |
| `evaluations` | Fixtures, graders, and regression runners | public contracts from other packages |
| `cli` / `api` | Validate transport input and invoke application services | public contracts; never adapter internals |

Dependencies should point inward. Domain packages must not import from `cli`,
`api`, or a concrete vendor adapter. Integrations are selected through
configuration and injected through protocols rather than global state.

## Planned execution path

```text
CLI / REST / Python caller
          |
          v
   skill registry + runner
          |
          +--> retrieve Customer Memory (with source references)
          +--> invoke configured model provider
          +--> validate structured output
          +--> append memory events (never replace history)
          +--> render optional artifacts
          v
 structured skill response
```

The diagram describes the target boundary interactions, not functionality in
the current scaffold.

## Customer Memory invariants

The Memory Engine milestone must preserve these invariants:

1. Facts and events are scoped to a customer identifier.
2. Updates append immutable revisions rather than overwriting earlier records.
3. Derived claims retain source references, timestamps, and confidence.
4. Retrieval can combine structured filters with semantic search.
5. Storage implementations are replaceable (SQLite locally, PostgreSQL in
   production) without changing skills.

## Extension strategy

- A **skill** implements the SDK contract and declares reads, writes, artifacts,
  prompts, and evaluations in metadata.
- A **connector** emits normalized records and never leaks vendor payloads into
  domain logic.
- A **model provider** implements a core inference protocol.
- An **artifact renderer** accepts a format-neutral document model; OfficeCLI is
  the preferred Office adapter.
- A **workflow** composes registered skills without reaching into their internal
  implementations.

Detailed contracts are deliberately deferred to their corresponding milestones
so the first vertical slice can validate them before they are declared stable.

