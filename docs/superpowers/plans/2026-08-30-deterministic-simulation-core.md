# Deterministic Simulation Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a network-free, replayable simulation engine that executes 18 multi-step Customer Success journeys and blocks merges on provenance, isolation, state, artifact, rollback, idempotency, or recovery regressions.

**Architecture:** Add a versioned JSON scenario DSL and an isolated `SimulationWorld` around the existing CSAF runtime. Inject deterministic time and IDs through existing composition roots, execute typed steps, capture canonical snapshots, grade hard contracts, and emit JSON/Markdown/JUnit reports without replacing `csaf.evaluations`.

**Tech Stack:** Python 3.11/3.12, Pydantic 2, SQLite, Typer, pytest, stdlib JSON/XML/tempfile/hashlib, existing CSAF runtime and Office abstractions.

---

## File Map

- `src/csaf/core/clock.py`: injectable UTC clock contract.
- `src/csaf/simulations/schema.py`: versioned scenario, step, expectation, and result models.
- `src/csaf/simulations/loader.py`: deterministic JSON discovery and validation.
- `src/csaf/simulations/world.py`: isolated runtime, clock, renderer, connector, artifacts, and snapshots.
- `src/csaf/simulations/faults.py`: named deterministic fault state.
- `src/csaf/simulations/runner.py`: ordered step execution and replay.
- `src/csaf/simulations/graders.py`: hard state, provenance, isolation, artifact, and policy assertions.
- `src/csaf/simulations/reporting.py`: JSON, Markdown, and JUnit serializers.
- `src/csaf/simulations/__init__.py`: supported public API.
- `evaluations/simulations/*.json`: 18 versioned journeys.
- `evaluations/simulations/fixtures/`: synthetic transcripts, connector pages, templates, and provenance.
- `tests/simulations/`: focused TDD coverage.
- `src/csaf/cli/app.py`: `csaf simulate` command.
- `.github/workflows/ci.yml`: merge-blocking deterministic suite.
- `docs/testing/simulations.md`: authoring, privacy, replay, and triage guide.

### Task 1: Inject deterministic time and IDs

**Files:**
- Create: `src/csaf/core/clock.py`
- Modify: `src/csaf/core/runtime.py`
- Modify: `src/csaf/memory/sqlite.py`
- Modify: `src/csaf/skills/base.py`
- Modify: `src/csaf/skills/runner.py`
- Modify: `src/csaf/skills/builtin/account_brief.py`
- Modify: `src/csaf/skills/builtin/qbr.py`
- Test: `tests/simulations/test_determinism.py`

- [ ] **Step 1: Write failing deterministic-runtime test**

```python
from datetime import UTC, datetime
from uuid import UUID

from csaf.core import create_runtime


def test_runtime_uses_injected_time_and_ids() -> None:
    instant = datetime(2026, 7, 1, 9, tzinfo=UTC)
    ids = iter(
        (
            UUID("00000000-0000-0000-0000-000000000001"),
            UUID("00000000-0000-0000-0000-000000000002"),
            UUID("00000000-0000-0000-0000-000000000003"),
        )
    )
    runtime = create_runtime(now=lambda: instant, id_factory=lambda: next(ids))
    try:
        result = runtime.runner.run("account-brief", {"customer_id": "acme"})
        assert result.execution_id == UUID("00000000-0000-0000-0000-000000000001")
        assert result.started_at == result.completed_at == instant
        assert result.output.generated_at == instant
        assert result.memory_updates[0].id == UUID(
            "00000000-0000-0000-0000-000000000002"
        )
        assert result.memory_updates[0].created_at == instant
    finally:
        runtime.memory.close()
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/simulations/test_determinism.py -v`

Expected: FAIL because `create_runtime` does not accept `now` or `id_factory`.

- [ ] **Step 3: Add injectable contracts and thread them through runtime**

```python
# src/csaf/core/clock.py
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

Now = Callable[[], datetime]
IdFactory = Callable[[], UUID]


def utc_now() -> datetime:
    return datetime.now(UTC)
```

Update `SQLiteMemoryStore.__init__` to accept `now: Now = utc_now` and
`id_factory: IdFactory = uuid4`, store both, and use them in `append`. Update
`SkillRunner.__init__` identically and use them for execution IDs and timestamps.
Add `now: datetime` to `SkillContext`. Replace direct `datetime.now(UTC)` calls in
Account Brief and QBR with `context.now`. Update `create_runtime`:

```python
def create_runtime(
    database: str | Path = ":memory:",
    office_renderer: OfficeArtifactRenderer | None = None,
    *,
    now: Now = utc_now,
    id_factory: IdFactory = uuid4,
) -> Runtime:
    memory = SQLiteMemoryStore(database, now=now, id_factory=id_factory)
    skills = SkillRegistry()
    skills.register(AccountBriefSkill())
    skills.register(MeetingCopilotSkill())
    skills.register(QBRSkill(office_renderer or OfficeCLIArtifactRenderer()))
    return Runtime(memory=memory, skills=skills, runner=SkillRunner(skills, memory, now, id_factory))
```

- [ ] **Step 4: Run focused and existing runtime tests**

Run: `python -m pytest tests/simulations/test_determinism.py tests/skills tests/memory -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/csaf/core src/csaf/memory/sqlite.py src/csaf/skills tests/simulations/test_determinism.py
git commit -m "refactor(core): inject deterministic runtime state"
```

### Task 2: Define versioned scenario DSL

**Files:**
- Create: `src/csaf/simulations/schema.py`
- Create: `src/csaf/simulations/__init__.py`
- Test: `tests/simulations/test_schema.py`

- [ ] **Step 1: Write failing schema tests**

```python
import pytest
from pydantic import ValidationError

from csaf.simulations import SimulationScenario


def test_scenario_validates_typed_steps() -> None:
    scenario = SimulationScenario.model_validate(
        {
            "schema_version": 1,
            "id": "sparse-account",
            "title": "Sparse account",
            "seed": 7,
            "customers": ["acme"],
            "steps": [
                {"type": "seed_memory", "records": []},
                {
                    "type": "run_skill",
                    "skill": "account-brief",
                    "input": {"customer_id": "acme"},
                },
            ],
            "expectations": [
                {"type": "output_equals", "path": "executive_summary", "value": "x"}
            ],
        }
    )
    assert scenario.steps[1].type == "run_skill"


def test_scenario_rejects_unknown_fields_and_duplicate_step_ids() -> None:
    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(
            {
                "schema_version": 1,
                "id": "bad",
                "title": "Bad",
                "seed": 1,
                "customers": ["acme"],
                "steps": [
                    {"id": "same", "type": "advance_time", "seconds": 1},
                    {"id": "same", "type": "advance_time", "seconds": 1},
                ],
                "expectations": [],
                "unexpected": True,
            }
        )
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/simulations/test_schema.py -v`

Expected: FAIL because `csaf.simulations` does not exist.

- [ ] **Step 3: Implement strict discriminated models**

Create `schema.py` with `ConfigDict(extra="forbid", frozen=True)` models:

```python
class SeedMemoryStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str | None = None
    type: Literal["seed_memory"]
    records: tuple[MemoryRecordCreate, ...]


class RunSkillStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str | None = None
    type: Literal["run_skill"]
    skill: str
    input: dict[str, JsonValue]
    expect_error: str | None = None


class AdvanceTimeStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str | None = None
    type: Literal["advance_time"]
    seconds: int = Field(gt=0, le=31_536_000)


class SetFaultStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str | None = None
    type: Literal["set_fault"]
    fault: Literal[
        "office_missing",
        "office_render_failure",
        "artifact_commit_failure",
        "connector_timeout",
        "connector_rate_limit",
        "corrupt_template",
    ]
    remaining_calls: int = Field(default=1, ge=1, le=100)


class ClearFaultsStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str | None = None
    type: Literal["clear_faults"]


class IngestFixtureStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str | None = None
    type: Literal["ingest_fixture"]
    customer_id: str
    fixture: str


SimulationStep = Annotated[
    SeedMemoryStep
    | RunSkillStep
    | AdvanceTimeStep
    | SetFaultStep
    | ClearFaultsStep
    | IngestFixtureStep,
    Field(discriminator="type"),
]
```

Define expectations for `output_equals`, `output_present`, `forbidden_term`,
`memory_count`, `memory_revision`, `artifact_types`, `citation_minimum`,
`no_cross_customer_data`, and `no_partial_effects` as a discriminated union.
`SimulationScenario` validates schema version `1`, kebab-case ID, non-empty unique
customers, unique non-null step IDs, and at least one step and expectation.

- [ ] **Step 4: Export public types and run tests**

Run: `python -m pytest tests/simulations/test_schema.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/csaf/simulations tests/simulations/test_schema.py
git commit -m "feat(simulations): add journey schema"
```

### Task 3: Load scenarios and validate fixture boundaries

**Files:**
- Create: `src/csaf/simulations/loader.py`
- Test: `tests/simulations/test_loader.py`

- [ ] **Step 1: Write failing loader tests**

```python
import json
from pathlib import Path

import pytest

from csaf.simulations.loader import SimulationDatasetError, load_scenarios


def test_loader_sorts_files_and_rejects_duplicate_ids(tmp_path: Path) -> None:
    base = {
        "schema_version": 1,
        "title": "Case",
        "seed": 1,
        "customers": ["acme"],
        "steps": [{"type": "advance_time", "seconds": 1}],
        "expectations": [{"type": "memory_count", "customer_id": "acme", "count": 0}],
    }
    (tmp_path / "b.json").write_text(json.dumps({**base, "id": "b"}))
    (tmp_path / "a.json").write_text(json.dumps({**base, "id": "a"}))
    assert [case.id for case in load_scenarios(tmp_path)] == ["a", "b"]
    (tmp_path / "b.json").write_text(json.dumps({**base, "id": "a"}))
    with pytest.raises(SimulationDatasetError, match="scenario IDs must be unique"):
        load_scenarios(tmp_path)
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/simulations/test_loader.py -v`

Expected: FAIL because loader is missing.

- [ ] **Step 3: Implement loader**

`load_scenarios(path)` accepts one JSON file or directory, sorts `*.json`, rejects
missing paths, non-UTF-8, malformed JSON, arrays, duplicate IDs, unsupported schema
versions, and fixture paths that are absolute or contain `..`. Wrap all failures in
`SimulationDatasetError` with source filename.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/simulations/test_loader.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/csaf/simulations/loader.py tests/simulations/test_loader.py
git commit -m "feat(simulations): load journey datasets"
```

### Task 4: Build isolated world, faults, and canonical snapshots

**Files:**
- Create: `src/csaf/simulations/faults.py`
- Create: `src/csaf/simulations/world.py`
- Test: `tests/simulations/test_world.py`

- [ ] **Step 1: Write failing isolation and fault tests**

```python
from datetime import UTC, datetime

import pytest

from csaf.office import OfficeCLIError, OfficeFormat, OfficeRenderRequest, OfficeSection
from csaf.simulations.world import SimulationWorld


def test_world_is_deterministic_and_isolated(tmp_path) -> None:
    first = SimulationWorld.create(tmp_path / "first", datetime(2026, 1, 1, tzinfo=UTC), 9)
    second = SimulationWorld.create(tmp_path / "second", datetime(2026, 1, 1, tzinfo=UTC), 9)
    try:
        one = first.runtime.runner.run("account-brief", {"customer_id": "acme"})
        two = second.runtime.runner.run("account-brief", {"customer_id": "acme"})
        assert first.canonical_result(one) == second.canonical_result(two)
        assert first.database_path != second.database_path
    finally:
        first.close()
        second.close()


def test_office_fault_fails_exact_number_of_calls(tmp_path) -> None:
    world = SimulationWorld.create(tmp_path, datetime(2026, 1, 1, tzinfo=UTC), 1)
    world.faults.set("office_render_failure", remaining_calls=1)
    request = OfficeRenderRequest(
        format=OfficeFormat.WORD,
        title="QBR",
        sections=(OfficeSection(title="Summary"),),
    )
    with pytest.raises(OfficeCLIError, match="simulated office render failure"):
        world.office.render(request)
    assert world.office.render(request).startswith(b"simulation:")
    world.close()
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/simulations/test_world.py -v`

Expected: FAIL because world and fault registry are missing.

- [ ] **Step 3: Implement isolated dependencies**

Create `FaultRegistry` with `set`, `clear`, and `consume` methods. Create:

```python
@dataclass
class MutableClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)
```

`SimulationOfficeRenderer` records requests, consumes Office faults, and returns
`b"simulation:<format>:<operation>:<title>"`. `SimulationWorld.create` resolves the
workspace, creates a SQLite database inside it, constructs a deterministic UUID5
factory from `(seed, counter)`, and calls `create_runtime` with injected time, IDs,
and renderer. It exposes `seed`, `memory_snapshot`, `canonical_result`,
`write_artifacts`, and `close`. Canonicalization removes volatile paths but does not
remove IDs or timestamps because those are deterministic.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/simulations/test_world.py tests/simulations/test_determinism.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/csaf/simulations/faults.py src/csaf/simulations/world.py tests/simulations
git commit -m "feat(simulations): isolate journey runtime"
```

### Task 5: Execute steps and preserve failure evidence

**Files:**
- Create: `src/csaf/simulations/runner.py`
- Test: `tests/simulations/test_journey_runner.py`

- [ ] **Step 1: Write failing journey test**

```python
from csaf.simulations import SimulationScenario
from csaf.simulations.runner import JourneyRunner
from csaf.simulations.world import SimulationWorld


def test_runner_captures_every_step_and_expected_error(world: SimulationWorld) -> None:
    scenario = SimulationScenario.model_validate(
        {
            "schema_version": 1,
            "id": "office-recovery",
            "title": "Office recovery",
            "seed": 2,
            "customers": ["acme"],
            "steps": [
                {"id": "fault", "type": "set_fault", "fault": "office_render_failure"},
                {
                    "id": "failed-qbr",
                    "type": "run_skill",
                    "skill": "qbr",
                    "input": {"customer_id": "acme", "quarter": "2026-Q3"},
                    "expect_error": "QBR artifact rendering failed",
                },
                {"id": "clear", "type": "clear_faults"},
                {
                    "id": "qbr",
                    "type": "run_skill",
                    "skill": "qbr",
                    "input": {"customer_id": "acme", "quarter": "2026-Q3"},
                },
            ],
            "expectations": [{"type": "artifact_types", "values": ["powerpoint", "word"]}],
        }
    )
    result = JourneyRunner(world).run(scenario)
    assert [step.id for step in result.steps] == ["fault", "failed-qbr", "clear", "qbr"]
    assert result.steps[1].error is not None
    assert result.steps[3].artifacts
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/simulations/test_journey_runner.py -v`

Expected: FAIL because runner is missing.

- [ ] **Step 3: Implement runner and result models**

Add `StepResult`, `SimulationSnapshot`, and `SimulationRun` to `schema.py`.
`JourneyRunner.run` dispatches every step type, captures snapshots before and after,
requires expected errors to occur and match, treats unexpected errors as failed steps,
never swallows cleanup errors, and returns evidence for grading. `IngestFixtureStep`
uses a `FixtureConnector` whose page sequence comes from validated JSON under the
scenario fixture root; connector faults raise `ConnectorError` before any page append.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/simulations/test_journey_runner.py tests/connectors -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/csaf/simulations tests/simulations/test_journey_runner.py
git commit -m "feat(simulations): execute journey steps"
```

### Task 6: Add hard graders

**Files:**
- Create: `src/csaf/simulations/graders.py`
- Test: `tests/simulations/test_graders.py`

- [ ] **Step 1: Write mutation-based failing tests**

```python
from csaf.simulations.graders import DeterministicGrader


def test_grader_detects_cross_customer_leak(passing_run) -> None:
    leaked = passing_run.model_copy(
        update={
            "serialized_outputs": passing_run.serialized_outputs
            + ('{"customer_id":"globex"}',)
        }
    )
    findings = DeterministicGrader().grade(leaked)
    assert any(f.code == "cross_customer_data" and not f.passed for f in findings)


def test_grader_detects_missing_citation_and_partial_effects(passing_run) -> None:
    mutated = passing_run.model_copy(
        update={"last_output": {"risks": [{"text": "Uncited risk"}]}}
    )
    findings = DeterministicGrader().grade(mutated)
    assert any(f.code == "citation_minimum" and not f.passed for f in findings)
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/simulations/test_graders.py -v`

Expected: FAIL because grader is missing.

- [ ] **Step 3: Implement expectation dispatch**

Create immutable `GradeFinding(code, passed, message, step_id)` and
`SimulationGrade(passed, findings)`. Resolve dotted output paths using the existing
evaluation semantics. Implement every expectation from Task 2. Citation grading
counts `memory_record_id`, `sources`, and transcript `excerpt` only when non-empty.
`no_partial_effects` compares failed-step snapshots. `no_cross_customer_data` checks
structured outputs, artifact text, and writes against scenario customer boundaries.
Artifact grading checks type order, safe filenames, media types, and SHA-256 content.

- [ ] **Step 4: Run mutation and full simulation tests**

Run: `python -m pytest tests/simulations -v`

Expected: PASS, including tests proving each planted mutation fails its intended grader.

- [ ] **Step 5: Commit**

```bash
git add src/csaf/simulations/graders.py src/csaf/simulations/schema.py tests/simulations
git commit -m "feat(simulations): grade hard contracts"
```

### Task 7: Add reports and CLI replay

**Files:**
- Create: `src/csaf/simulations/reporting.py`
- Modify: `src/csaf/cli/app.py`
- Modify: `src/csaf/simulations/__init__.py`
- Test: `tests/simulations/test_reporting.py`
- Test: `tests/transports/test_cli.py`

- [ ] **Step 1: Write failing report and CLI tests**

```python
def test_junit_escapes_failure_and_replay_is_stable(failed_report) -> None:
    xml = render_junit(failed_report)
    assert "&lt;secret&gt;" in xml
    assert failed_report.replay_command == (
        "csaf simulate evaluations/simulations --scenario office-recovery --seed 2"
    )


def test_simulate_cli_writes_all_reports(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "simulate",
            "evaluations/simulations",
            "--scenario",
            "sparse-account",
            "--report-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert (tmp_path / "simulation-report.json").is_file()
    assert (tmp_path / "simulation-report.md").is_file()
    assert (tmp_path / "simulation-junit.xml").is_file()
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/simulations/test_reporting.py tests/transports/test_cli.py -k simulate -v`

Expected: FAIL because reporters and command are missing.

- [ ] **Step 3: Implement serializers and command**

Add top-level `csaf simulate DATASET` with `--scenario`, `--seed`, `--report-dir`,
and `--fixture-root`. Default seed comes from scenario. Exit `0` for pass, `1` for a
valid failed simulation, and `2` for dataset/runtime errors. Reports use canonical
sorted JSON, Markdown tables, and stdlib `xml.etree.ElementTree`. Apply
`redact_officecli_message` plus a simulation-specific email/domain/credential redactor
before serialization. Never place raw secrets in replay commands.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/simulations tests/transports/test_cli.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/csaf/simulations src/csaf/cli/app.py tests/simulations tests/transports/test_cli.py
git commit -m "feat(cli): report simulation journeys"
```

### Task 8: Add 18 deterministic journeys and synthetic fixtures

**Files:**
- Create: `evaluations/simulations/lifecycle.json`
- Create: `evaluations/simulations/meetings.json`
- Create: `evaluations/simulations/integrity.json`
- Create: `evaluations/simulations/fixtures/noisy-meeting.txt`
- Create: `evaluations/simulations/fixtures/conflicting-commitments.txt`
- Create: `evaluations/simulations/fixtures/support-timeout.json`
- Create: `evaluations/simulations/fixtures/provenance.json`
- Test: `tests/simulations/test_dataset.py`

- [ ] **Step 1: Write failing catalog test**

```python
def test_bundled_catalog_has_approved_coverage() -> None:
    scenarios = load_scenarios("evaluations/simulations")
    assert len(scenarios) == 18
    assert {scenario.id for scenario in scenarios} == {
        "new-customer-sparse-memory",
        "healthy-adoption-growth",
        "declining-usage-before-renewal",
        "executive-sponsor-departure",
        "security-review-blocking-rollout",
        "escalated-support-incident",
        "expansion-with-incomplete-evidence",
        "multi-quarter-qbr-progression",
        "noisy-meeting-transcript",
        "conflicting-speaker-commitments",
        "missing-owner-and-date",
        "duplicate-meeting-retry",
        "competitor-mention-not-churn",
        "tenant-isolation-attack",
        "fresh-evidence-overrides-stale",
        "connector-timeout-retry",
        "corrupt-qbr-template",
        "officecli-consent-recovery",
    }
    assert all(scenario.expectations for scenario in scenarios)
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/simulations/test_dataset.py -v`

Expected: FAIL with missing dataset.

- [ ] **Step 3: Add lifecycle scenarios**

`lifecycle.json` contains scenarios 1-8. Each uses explicit 2026 timestamps and fixed
seeds `101` through `108`. Every factual fixture includes a `SourceReference`. Run
Meeting Copilot before Account Brief in scenarios 3-7 and QBR after both in scenarios
3, 5, and 8. Assert exact memory kinds, logical-key revisions, citation minimums,
artifact types, missing-value behavior, and absence of fictional competitors.

- [ ] **Step 4: Add meeting scenarios**

`meetings.json` contains scenarios 9-13 with fixed seeds `201` through `205`.
Fixtures include filler, interruptions, contradictory speakers, missing owners/dates,
duplicate delivery, and competitor wording. Assert source excerpts, no invented owner
or date, stable logical keys across retry, and no automatic risk created from a bare
competitor mention.

- [ ] **Step 5: Add integrity and failure scenarios**

`integrity.json` contains scenarios 14-18 with seeds `301` through `305`. Assert zero
Globex content in Acme results, newer logical-key revision selection where skills use
latest memory, connector retry writes each external ID once, corrupt templates leave
no QBR effects, and denied Office installation produces no changes before a later
approved deterministic recovery fixture.

- [ ] **Step 6: Add fixture provenance**

```json
{
  "schema_version": 1,
  "fixtures": [
    {
      "path": "noisy-meeting.txt",
      "source_class": "synthetic",
      "pii_reviewed": true,
      "reviewed_on": "2026-08-30",
      "permitted_use": "public-ci"
    },
    {
      "path": "conflicting-commitments.txt",
      "source_class": "synthetic",
      "pii_reviewed": true,
      "reviewed_on": "2026-08-30",
      "permitted_use": "public-ci"
    },
    {
      "path": "support-timeout.json",
      "source_class": "synthetic",
      "pii_reviewed": true,
      "reviewed_on": "2026-08-30",
      "permitted_use": "public-ci"
    }
  ]
}
```

Add SHA-256 fields after files exist; loader verifies them.

- [ ] **Step 7: Run every deterministic journey twice**

Run: `python -m pytest tests/simulations/test_dataset.py -v`

Run: `csaf --database :memory: simulate evaluations/simulations --report-dir build/simulations`

Expected: 18/18 pass on both executions and canonical reports match except duration.

- [ ] **Step 8: Commit**

```bash
git add evaluations/simulations tests/simulations/test_dataset.py
git commit -m "test(simulations): add customer journeys"
```

### Task 9: Gate deterministic simulations in CI and document use

**Files:**
- Modify: `.github/workflows/ci.yml`
- Create: `docs/testing/simulations.md`
- Modify: `README.md`
- Test: `tests/test_documentation.py`
- Test: `tests/simulations/test_ci_contract.py`

- [ ] **Step 1: Write failing CI contract test**

```python
def test_ci_runs_deterministic_simulations_without_network() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text("utf-8")
    command = "csaf --database :memory: simulate evaluations/simulations"
    assert command in workflow
    assert "--report-dir simulation-results" in workflow
    assert workflow.index(command) < workflow.index("actions/upload-artifact@")
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/simulations/test_ci_contract.py tests/test_documentation.py -v`

Expected: FAIL because workflow and guide are absent.

- [ ] **Step 3: Add CI command and artifact upload**

Run deterministic simulations in both Python jobs after unit tests. Upload reports on
`always()` with the existing pinned `actions/upload-artifact` commit and
`retention-days: 14`. Do not add network credentials or external fixture downloads.

- [ ] **Step 4: Write normal-prose guide**

Document schema, step types, expectations, synthetic/sanitized fixture policy, PII
review, commands, exit codes, replay, failure classification, and how to add a journey.
README links to the guide and states deterministic simulations run on every pull request.

- [ ] **Step 5: Run documentation and CI contract tests**

Run: `python -m pytest tests/simulations/test_ci_contract.py tests/test_documentation.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml docs/testing/simulations.md README.md tests
git commit -m "ci: gate deterministic simulations"
```

### Task 10: Final verification

**Files:**
- Verify only

- [ ] **Step 1: Run simulation suite with warnings as errors**

Run: `python -m pytest tests/simulations -q -W error`

Expected: PASS.

- [ ] **Step 2: Run full project suite**

Run: `python -m pytest -q -W error`

Expected: all tests pass; Windows-only POSIX contract skips remain documented.

- [ ] **Step 3: Run lint, formatting, secret scan, and package build**

```bash
python -m ruff check .
python -m ruff format --check src tests
python scripts/check_secrets.py --worktree --tracked --history
python -m build --no-isolation
git diff --check
```

Expected: all commands exit `0`; wheel and source archive build.

- [ ] **Step 4: Verify installed wheel**

Install wheel into a fresh temporary virtual environment, then run:

```bash
csaf --database :memory: simulate evaluations/simulations --scenario new-customer-sparse-memory --report-dir simulation-smoke
```

Expected: exit `0` with JSON, Markdown, and JUnit reports.

- [ ] **Step 5: Commit verification-only adjustments if required**

If verification changes tracked files, commit only those targeted fixes with a
conventional message and rerun the failed gate. Otherwise create no empty commit.
