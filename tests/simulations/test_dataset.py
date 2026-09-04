"""Contract tests for bundled real-world simulation journeys."""

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from csaf.memory import SQLiteMemoryStore
from csaf.schemas import MemoryQuery, MemoryRecord
from csaf.simulations.graders import DeterministicGrader
from csaf.simulations.loader import load_scenarios
from csaf.simulations.reporting import (
    SimulationScenarioReport,
    SimulationSuiteReport,
    canonical_json,
)
from csaf.simulations.runner import JourneyRunner
from csaf.simulations.schema import (
    ClearFaultsStep,
    IngestFixtureStep,
    RunSkillStep,
    SeedMemoryStep,
    SetFaultStep,
)
from csaf.simulations.world import SimulationWorld

DATASET = Path("evaluations/simulations")
FIXTURES = DATASET / "fixtures"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
CATALOG = {
    "new-customer-sparse-memory": 101,
    "healthy-adoption-growth": 102,
    "declining-usage-before-renewal": 103,
    "executive-sponsor-departure": 104,
    "security-review-blocking-rollout": 105,
    "escalated-support-incident": 106,
    "expansion-with-incomplete-evidence": 107,
    "multi-quarter-qbr-progression": 108,
    "noisy-meeting-transcript": 201,
    "conflicting-speaker-commitments": 202,
    "missing-owner-and-date": 203,
    "duplicate-meeting-retry": 204,
    "competitor-mention-not-churn": 205,
    "tenant-isolation-attack": 301,
    "fresh-evidence-overrides-stale": 302,
    "connector-timeout-retry": 303,
    "corrupt-qbr-template": 304,
    "officecli-consent-recovery": 305,
}
FIXTURE_CATALOG = {
    "conflicting-commitments.txt",
    "escalated-incident.json",
    "healthy-adoption.json",
    "noisy-meeting.txt",
    "support-timeout.json",
}
LIFECYCLE_MEMORY_KINDS = {
    "new-customer-sparse-memory": {"artifact": 1, "profile": 1},
    "healthy-adoption-growth": {"artifact": 1, "health": 1, "product_usage": 1},
    "declining-usage-before-renewal": {
        "action_item": 1,
        "artifact": 3,
        "meeting": 1,
        "qbr": 1,
        "risk": 1,
        "timeline": 1,
    },
    "executive-sponsor-departure": {
        "artifact": 1,
        "commitment": 1,
        "meeting": 1,
        "risk": 1,
        "stakeholder": 1,
        "timeline": 1,
    },
    "security-review-blocking-rollout": {
        "artifact": 3,
        "commitment": 1,
        "meeting": 1,
        "qbr": 1,
        "risk": 1,
        "success_plan": 1,
        "timeline": 1,
    },
    "escalated-support-incident": {
        "artifact": 1,
        "commitment": 1,
        "meeting": 1,
        "risk": 1,
        "support": 1,
        "timeline": 1,
    },
    "expansion-with-incomplete-evidence": {
        "artifact": 1,
        "meeting": 1,
        "timeline": 1,
    },
    "multi-quarter-qbr-progression": {
        "action_item": 1,
        "artifact": 5,
        "commitment": 1,
        "meeting": 1,
        "product_usage": 2,
        "qbr": 2,
        "timeline": 1,
    },
}


def test_bundled_catalog_has_exact_approved_coverage() -> None:
    scenarios = load_scenarios(DATASET)

    assert len(scenarios) == len(CATALOG)
    assert {scenario.id: scenario.seed for scenario in scenarios} == CATALOG
    assert len(tuple(DATASET.glob("*.json"))) == len(CATALOG)
    assert all(scenario.expectations for scenario in scenarios)


def test_fixtures_are_bounded_synthetic_and_hash_verified() -> None:
    scenarios = load_scenarios(DATASET)
    manifest = json.loads((FIXTURES / "provenance.json").read_text("utf-8"))
    entries = {entry["path"]: entry for entry in manifest["fixtures"]}
    referenced = {
        step.fixture
        for scenario in scenarios
        for step in scenario.steps
        if isinstance(step, IngestFixtureStep)
    }

    assert manifest["schema_version"] == 1
    physical = {path.name for path in FIXTURES.iterdir() if path.name != "provenance.json"}
    assert set(entries) == physical == FIXTURE_CATALOG
    assert referenced <= set(entries)
    for relative, entry in entries.items():
        path = FIXTURES / relative
        assert path.is_file()
        assert path.resolve().is_relative_to(FIXTURES.resolve())
        assert entry["source_class"] == "synthetic"
        assert entry["pii_reviewed"] is True
        assert entry["reviewed_on"] == "2026-08-30"
        assert entry["permitted_use"] == "public-ci"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]


def test_fixture_hashes_use_repository_stable_line_endings() -> None:
    attributes = Path(".gitattributes").read_text("utf-8")

    assert "evaluations/simulations/fixtures/*.json text eol=lf" in attributes
    assert "evaluations/simulations/fixtures/*.txt text eol=lf" in attributes


def test_journeys_encode_real_world_ordering_and_explicit_2026_time() -> None:
    by_id = {scenario.id: scenario for scenario in load_scenarios(DATASET)}

    for scenario in by_id.values():
        timestamps = [
            record.occurred_at
            for step in scenario.steps
            if isinstance(step, SeedMemoryStep)
            for record in step.records
        ] + [
            step.input.get("occurred_at")
            for step in scenario.steps
            if isinstance(step, RunSkillStep) and "occurred_at" in step.input
        ]
        for step in scenario.steps:
            if isinstance(step, IngestFixtureStep):
                fixture = json.loads((FIXTURES / step.fixture).read_text("utf-8"))
                timestamps.extend(record.get("occurred_at") for record in fixture["records"])
        assert timestamps
        assert all(str(value).startswith("2026-") for value in timestamps)

    for scenario_id in (
        "declining-usage-before-renewal",
        "executive-sponsor-departure",
        "security-review-blocking-rollout",
        "escalated-support-incident",
        "expansion-with-incomplete-evidence",
    ):
        skills = [step.skill for step in by_id[scenario_id].steps if isinstance(step, RunSkillStep)]
        assert skills.index("meeting-copilot") < skills.index("account-brief")
    for scenario_id in (
        "declining-usage-before-renewal",
        "security-review-blocking-rollout",
        "multi-quarter-qbr-progression",
    ):
        skills = [step.skill for step in by_id[scenario_id].steps if isinstance(step, RunSkillStep)]
        assert skills.index("account-brief") < skills.index("qbr")

    progression = by_id["multi-quarter-qbr-progression"]
    progression_skills = [
        step.skill for step in progression.steps if isinstance(step, RunSkillStep)
    ]
    assert progression_skills == ["meeting-copilot", "account-brief", "qbr", "qbr"]

    for scenario_id, fixture in (
        ("noisy-meeting-transcript", "noisy-meeting.txt"),
        ("conflicting-speaker-commitments", "conflicting-commitments.txt"),
    ):
        meeting = next(
            step
            for step in by_id[scenario_id].steps
            if isinstance(step, RunSkillStep) and step.skill == "meeting-copilot"
        )
        assert meeting.input["transcript"] == (FIXTURES / fixture).read_text("utf-8").rstrip("\n")

    retry = by_id["connector-timeout-retry"]
    ingests = [step for step in retry.steps if isinstance(step, IngestFixtureStep)]
    assert len(ingests) == 2
    assert ingests[0].expect_error == "simulated connector timeout"
    assert ingests[1].expect_error is None


def test_fresh_evidence_wins_over_stale_revision_inside_60_day_window(tmp_path: Path) -> None:
    fresh = next(
        scenario
        for scenario in load_scenarios(DATASET)
        if scenario.id == "fresh-evidence-overrides-stale"
    )
    fresh_brief = next(
        step
        for step in fresh.steps
        if isinstance(step, RunSkillStep) and step.skill == "account-brief"
    )
    assert fresh_brief.input == {"customer_id": "acme", "time_window_days": 60}

    with SimulationWorld.create(tmp_path, EPOCH, fresh.seed) as world:
        run = JourneyRunner(world, fixture_root=FIXTURES).run(fresh)
    assert run.success
    brief = next(step for step in run.steps if step.id == fresh_brief.id)
    revisions = sorted(
        (record for record in brief.before.memory if record["logical_key"] == "risk:renewal"),
        key=lambda record: record["revision"],
    )
    assert [record["revision"] for record in revisions] == [1, 2]
    since = brief.started_at - timedelta(days=fresh_brief.input["time_window_days"])
    timestamps = [
        datetime.fromisoformat(record["occurred_at"].replace("Z", "+00:00")) for record in revisions
    ]
    assert since <= timestamps[0] < timestamps[1] <= brief.started_at
    assert len(brief.output["risks"]) == 1
    assert brief.output["risks"][0]["text"] == "Fresh: renewal blocker resolved."
    assert brief.output["risks"][0]["memory_record_id"] == revisions[1]["id"]
    assert "Stale: renewal is blocked." not in json.dumps(brief.output)


def test_tenant_isolation_briefs_retrieve_after_both_meetings(tmp_path: Path) -> None:
    scenario = next(
        item for item in load_scenarios(DATASET) if item.id == "tenant-isolation-attack"
    )
    skills = [step for step in scenario.steps if isinstance(step, RunSkillStep)]
    assert [(step.skill, step.input["customer_id"]) for step in skills] == [
        ("meeting-copilot", "acme"),
        ("meeting-copilot", "globex"),
        ("account-brief", "acme"),
        ("account-brief", "globex"),
    ]
    with SimulationWorld.create(tmp_path, EPOCH, scenario.seed) as world:
        run = JourneyRunner(world, fixture_root=FIXTURES).run(scenario)
    assert run.success
    assert DeterministicGrader().grade(scenario, run).passed
    for customer, other in (("acme", "globex"), ("globex", "acme")):
        brief = next(step for step in run.steps if step.id == f"{customer}-brief")
        assert {record["customer_id"] for record in brief.before.memory} == {"acme", "globex"}
        assert len(brief.output["action_items"]) == 1
        action = brief.output["action_items"][0]
        assert action["text"] == f"Validate {customer.title()} role mappings."
        own_ids = {
            record["id"] for record in brief.before.memory if record["customer_id"] == customer
        }
        assert action["memory_record_id"] in own_ids
        assert other not in json.dumps(brief.output).casefold()


@pytest.mark.parametrize("mutation", ["disabled", "cross-customer"])
def test_tenant_isolation_journey_rejects_retrieval_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    scenario = next(
        item for item in load_scenarios(DATASET) if item.id == "tenant-isolation-attack"
    )
    original_search = SQLiteMemoryStore.search
    calls = []

    def mutated_search(store: SQLiteMemoryStore, query: MemoryQuery) -> list[MemoryRecord]:
        calls.append(query.customer_id)
        if mutation == "disabled":
            raise AssertionError("tenant retrieval disabled")
        other = "globex" if query.customer_id == "acme" else "acme"
        return original_search(store, query) + original_search(
            store, query.model_copy(update={"customer_id": other})
        )

    monkeypatch.setattr(SQLiteMemoryStore, "search", mutated_search)
    with SimulationWorld.create(tmp_path, EPOCH, scenario.seed) as world:
        run = JourneyRunner(world, fixture_root=FIXTURES).run(scenario)
    grade = DeterministicGrader().grade(scenario, run)
    assert not grade.passed
    assert calls == (["acme"] if mutation == "disabled" else ["acme", "globex"])
    if mutation == "cross-customer":
        assert run.success
        assert any(
            finding.code == "no_cross_customer_data" and not finding.passed
            for finding in grade.findings
        )


def test_lifecycle_journeys_have_exact_final_memory_kinds(tmp_path: Path) -> None:
    by_id = {scenario.id: scenario for scenario in load_scenarios(DATASET)}

    for scenario_id, expected in LIFECYCLE_MEMORY_KINDS.items():
        scenario = by_id[scenario_id]
        with SimulationWorld.create(tmp_path / scenario_id, EPOCH, scenario.seed) as world:
            run = JourneyRunner(world, fixture_root=FIXTURES).run(scenario)
        actual = Counter(record["kind"] for record in run.final_snapshot.memory)
        assert actual == Counter(expected), scenario_id


def test_officecli_consent_journey_declares_core_proxy_semantics() -> None:
    """Core models availability; native-agent tests own real installer consent UI."""

    scenario = next(
        item for item in load_scenarios(DATASET) if item.id == "officecli-consent-recovery"
    )
    denied = next(step for step in scenario.steps if step.id == "denied-install")
    approved = next(step for step in scenario.steps if step.id == "approved-install")
    failed = next(step for step in scenario.steps if step.id == "failed-qbr")

    assert "proxy" in scenario.title.casefold()
    assert isinstance(denied, SetFaultStep) and denied.fault == "office_missing"
    assert isinstance(approved, ClearFaultsStep)
    assert isinstance(failed, RunSkillStep)
    assert failed.expect_error == "QBR artifact rendering failed"


def test_skills_never_consume_future_dated_evidence(tmp_path: Path) -> None:
    for scenario in load_scenarios(DATASET):
        with SimulationWorld.create(tmp_path / scenario.id, EPOCH, scenario.seed) as world:
            run = JourneyRunner(world, fixture_root=FIXTURES).run(scenario)
        declared = {
            step.id or f"step-{index}": step for index, step in enumerate(scenario.steps, start=1)
        }
        for result in run.steps:
            step = declared[result.id]
            if not isinstance(step, RunSkillStep):
                continue
            occurred_at = step.input.get("occurred_at")
            if isinstance(occurred_at, str):
                assert (
                    datetime.fromisoformat(occurred_at.replace("Z", "+00:00")) <= result.started_at
                )
            for record in result.before.memory:
                record_time = record.get("occurred_at")
                if isinstance(record_time, str):
                    assert (
                        datetime.fromisoformat(record_time.replace("Z", "+00:00"))
                        <= result.started_at
                    )


def test_every_journey_replays_identically_and_passes(tmp_path: Path) -> None:
    grader = DeterministicGrader()

    for scenario in load_scenarios(DATASET):
        evidence = []
        for replay in range(2):
            with SimulationWorld.create(
                tmp_path / scenario.id / str(replay), EPOCH, scenario.seed
            ) as world:
                run = JourneyRunner(world, fixture_root=FIXTURES).run(scenario)
            grade = grader.grade(scenario, run)
            assert run.success, scenario.id
            assert grade.passed, (scenario.id, grade.findings)
            if scenario.id in {"corrupt-qbr-template", "officecli-consent-recovery"}:
                failed_qbr = next(step for step in run.steps if step.id == "failed-qbr")
                assert failed_qbr.before.memory == failed_qbr.after.memory
                assert failed_qbr.before.artifacts == failed_qbr.after.artifacts
                assert failed_qbr.before.office_requests == failed_qbr.after.office_requests
            report = SimulationScenarioReport(
                scenario_id=scenario.id,
                scenario_title=scenario.title,
                seed=scenario.seed,
                run=run,
                grade=grade,
                passed=run.success and grade.passed,
                replay_argv=(
                    "csaf",
                    "simulate",
                    DATASET.as_posix(),
                    "--scenario",
                    scenario.id,
                    "--seed",
                    str(scenario.seed),
                    "--fixture-root",
                    FIXTURES.as_posix(),
                ),
            )
            suite = SimulationSuiteReport(
                schema_version=1,
                started_at=EPOCH,
                config={"fixture_root": "bundled"},
                total=1,
                passed_count=1,
                failed_count=0,
                passed=True,
                scenarios=(report,),
            )
            evidence.append(canonical_json(suite))
        assert evidence[0] == evidence[1], scenario.id
