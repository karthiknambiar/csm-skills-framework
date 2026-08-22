# Customer Success Agent Framework (CSAF)

**This is still work in progress and not the final form.**

CSAF is an open-source, vendor-neutral foundation for reusable AI skills for
Customer Success Managers, Technical Account Managers, Solutions Engineers,
and other customer-facing teams.

The framework is **memory first**: skills will read structured customer context,
perform one focused task, return structured results, and append newly learned
information to Customer Memory. It is designed to remain model-, connector-,
and artifact-provider agnostic.

> **Project status:** The ten planned foundation milestones are complete. The
> project includes Customer Memory, three built-in skills, transports, Office and
> connector boundaries, deterministic evaluation, documentation, and examples.

## Repository layout

The Python package uses a `src` layout. Each top-level package represents a
stable architectural boundary:

```text
src/csaf/
├── api/          # REST transport (Milestone 4)
├── cli/          # Command-line transport (Milestone 4)
├── connectors/   # Vendor-neutral ingestion adapters (Milestone 8)
├── core/         # Shared runtime primitives and configuration
├── evaluations/  # Skill quality and regression evaluation (Milestone 9)
├── memory/       # Append-only Customer Memory (Milestone 2)
├── office/       # OfficeCLI-backed artifact generation (Milestone 7)
├── prompts/      # Versioned prompt assets
├── schemas/      # Shared Pydantic contracts (Milestone 2)
├── skills/       # Reusable skill implementations and SDK (Milestone 3)
├── templates/    # Artifact templates
└── workflows/    # Composition of multiple skills
```

See [Architecture](docs/architecture.md) for dependency rules and
[Milestones](docs/milestones.md) for the incremental delivery plan.

## Documentation

The [documentation index](docs/index.md) links the architecture, tutorial, memory
model, CLI and REST references, extension guides, evaluation framework,
compatibility policy, and contribution workflow. Runnable scripts and canonical
sample inputs are available under [`examples/`](examples/README.md).

## Development

CSAF requires Python 3.11 or newer. Install the development extra into an
isolated environment; it includes test, lint, and package-build tooling:

```bash
python -m venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/python -m build
```

Windows PowerShell users can replace `.venv/bin/python` with
`.\.venv\Scripts\python.exe`. See [Contributing](CONTRIBUTING.md) for the full
verification and secret-scanning workflow.

## Customer Memory quick start

The development backend persists immutable, customer-scoped revisions in
SQLite. Use an in-memory database for tests or pass a file path for persistence:

```python
from csaf.memory import SQLiteMemoryStore
from csaf.schemas import MemoryKind, MemoryQuery, MemoryRecordCreate

with SQLiteMemoryStore("customers.db") as memory:
    memory.append(
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.RISK,
            logical_key="risk:migration",
            content="The production migration is delayed.",
            confidence=0.9,
        )
    )
    current_risks = memory.search(
        MemoryQuery(
            customer_id="acme",
            kinds=(MemoryKind.RISK,),
            latest_only=True,
        )
    )
```

Appending the same `logical_key` creates a new revision; earlier revisions stay
available through `memory.history(customer_id, logical_key)`.

## Skills SDK

Skills subclass `Skill`, declare a stable `SkillMetadata` contract, and provide
Pydantic input and output models. `SkillRunner` owns the standard lifecycle:

1. Validate structured input.
2. Retrieve the declared Customer Memory categories.
3. Execute the skill with an isolated `SkillContext`.
4. Validate structured output and declared effects.
5. Deliver artifacts before appending memory updates.
6. Append memory updates and return a `SkillRunResult`.

The runner prevents undeclared memory writes and artifacts as well as writes to
a customer other than the one in the input. A complete minimal authoring example
is available in `tests/skills/test_sdk.py`; the Account Brief milestone will be
the first production skill built on this SDK.

See the [Skill development guide](docs/skill-development.md) for the authoring
contract and a minimal implementation.

## CLI and REST API

Both transports use the same runtime, skill registry, runner, and memory store.
The CLI defaults to `csaf.db`; set `CSAF_DATABASE` or pass `--database` to choose
another SQLite file.

```bash
csaf skills list
csaf skill run account-brief --input '{"customer_id":"acme"}'
csaf memory inspect acme --kind risk --latest-only
```

Start the development REST server with:

```bash
uvicorn 'csaf.api:create_app' --factory --reload
```

Current transport operations are `GET /health`, `GET /skills`,
`POST /skills/{skill_name}`, and `GET /customer/{customer_id}/memory`. Built-in
skill routes become usable as those skills land in their planned milestones.

## Account Brief

Account Brief is the first complete vertical slice. It reads only declared,
customer-scoped memory categories; returns structured, cited evidence; creates a
Markdown artifact; and appends a versioned generation record to Customer Memory.
It deliberately uses deterministic synthesis so missing context is reported
rather than invented. Model-backed synthesis can be added behind a provider
contract without changing its public input or output schemas.

```bash
csaf account-brief acme --days 90 --output acme-brief.md
```

```bash
curl -X POST http://localhost:8000/skills/account-brief \
  -H 'content-type: application/json' \
  -d '{"input":{"customer_id":"acme","time_window_days":90}}'
```

## Meeting Copilot

Meeting Copilot analyzes a speaker-labeled transcript into grounded summaries,
goals, actions, blockers, commitments, risks, sentiment, competitor mentions,
and product feedback. It also returns a follow-up email and CRM notes, produces a
Markdown analysis, and appends `meeting`, `timeline`, `action_item`, `commitment`,
`risk`, and `feature_request` records with transcript provenance.

```bash
csaf meeting analyze transcript.md \
  --customer-id acme \
  --meeting-id meeting-42 \
  --attendee Alex \
  --attendee Priya \
  --output meeting-42-analysis.md
```

The initial extractor is deliberately deterministic and explainable: every
finding retains its exact transcript excerpt and speaker. A later model-provider
adapter can enhance classification while preserving these public contracts and
provenance requirements.

## QBR generation

The QBR skill assembles a cited executive summary, adoption trends, support
metrics, business outcomes, roadmap, recommendations, goals, and next-quarter
plan from Customer Memory. It uses the OfficeCLI adapter to create or update both
PowerPoint and Word artifacts, and appends versioned QBR and artifact records.

```bash
csaf qbr generate acme --quarter 2026-Q3 --output-dir artifacts/
```

Update existing files instead of recreating them:

```bash
csaf qbr generate acme --quarter 2026-Q3 \
  --existing-powerpoint artifacts/acme-2026-Q3-qbr-v1.pptx \
  --existing-word artifacts/acme-2026-Q3-qbr-v1.docx \
  --output-dir artifacts/
```

QBR artifacts require fully local, deterministic
[`iOfficeAI/OfficeCLI`](https://github.com/iOfficeAI/OfficeCLI) 1.0.137 or newer.
CSAF never installs it, calls a hosted model, or asks for an API key. Verify the
local executable and temporary PPTX/DOCX smoke renders with `csaf office doctor`;
automation can use `csaf office doctor --json`. Installation, supported command
flow, and migration guidance are in the
[OfficeCLI integration guide](docs/officecli.md).

## Connector framework

Connectors advertise discovery and authentication metadata, fetch stable pages,
normalize vendor data, and produce resumable checkpoints. `ConnectorIngestor`
owns customer scoping, provenance, and append-only memory writes. Markdown, JSON,
and CSV reference connectors are included:

```bash
csaf connector ingest markdown customer-notes/ --customer-id acme
csaf connector ingest json crm-export.json --customer-id acme \
  --checkpoint-file .checkpoints/acme.json
csaf connector ingest csv usage.csv --customer-id acme
```

See the [Connector development guide](docs/connector-development.md) for the
authoring contract, canonical formats, pagination rules, and conformance tests.

## Evaluation framework

Golden cases seed isolated Customer Memory, execute a skill twice, and score
accuracy, completeness, hallucination checks, citations, consistency, and memory
effects. The bundled Account Brief, Meeting Copilot, and QBR regressions run in CI:

```bash
csaf evaluate evaluations/golden --report evaluation-report.json
```

The command exits nonzero when a score falls below its versioned threshold. See
the [Evaluation guide](docs/evaluations.md) for the case schema, scoring rules,
report format, and guidance for intentional baseline changes.

## Principles

- **Memory first:** preserve provenance and history; never silently overwrite it.
- **Skills over applications:** expose narrow, composable, typed capabilities.
- **Vendor neutral:** normalize model, connector, and storage integrations behind
  internal contracts.
- **Office native:** treat Markdown, Word, PowerPoint, and Excel as first-class
  versioned artifacts.
- **Evaluated by default:** ship each skill with fixtures and regression tests.

## Contributing

Review [CONTRIBUTING.md](CONTRIBUTING.md) before proposing a change. New runtime
features should follow the milestone sequence rather than coupling multiple
layers in one change.
