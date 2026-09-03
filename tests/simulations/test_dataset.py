"""Contract tests for bundled real-world simulation journeys."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from csaf.simulations.graders import DeterministicGrader
from csaf.simulations.loader import load_scenarios
from csaf.simulations.reporting import (
    SimulationScenarioReport,
    SimulationSuiteReport,
    canonical_json,
)
from csaf.simulations.runner import JourneyRunner
from csaf.simulations.schema import IngestFixtureStep, RunSkillStep, SeedMemoryStep
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
    assert referenced == set(entries)
    for relative, entry in entries.items():
        path = FIXTURES / relative
        assert path.is_file()
        assert path.resolve().is_relative_to(FIXTURES.resolve())
        assert entry["source_class"] == "synthetic"
        assert entry["pii_reviewed"] is True
        assert entry["reviewed_on"] == "2026-08-30"
        assert entry["permitted_use"] == "public-ci"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]


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

    retry = by_id["connector-timeout-retry"]
    ingests = [step for step in retry.steps if isinstance(step, IngestFixtureStep)]
    assert len(ingests) == 2
    assert ingests[0].expect_error == "simulated connector timeout"
    assert ingests[1].expect_error is None


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
