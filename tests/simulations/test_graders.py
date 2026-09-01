"""Hard-contract tests for deterministic simulation grading."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from csaf.simulations import (
    DeterministicGrader,
    JourneyRunner,
    SimulationScenario,
    SimulationWorld,
)

START = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)


def _scenario(expectations, *, customers=("acme",), steps=None):
    return SimulationScenario.model_validate(
        {
            "schema_version": 1,
            "id": "grader-contract",
            "title": "Grader contract",
            "seed": 7,
            "customers": customers,
            "steps": steps
            or [
                {
                    "id": "brief",
                    "type": "run_skill",
                    "skill": "account-brief",
                    "input": {"customer_id": "acme"},
                }
            ],
            "expectations": expectations,
        }
    )


def _run(tmp_path, scenario):
    with SimulationWorld.create(tmp_path / "world", START, scenario.seed) as world:
        return JourneyRunner(world).run(scenario)


@pytest.mark.parametrize(
    ("expectation", "passed"),
    [
        ({"type": "output_equals", "path": "customer_id", "value": "acme"}, True),
        ({"type": "output_equals", "path": "customer_id", "value": "other"}, False),
        ({"type": "output_present", "path": "customer_id"}, True),
        ({"type": "output_present", "path": "missing"}, False),
        ({"type": "forbidden_term", "term": "globex"}, True),
        ({"type": "forbidden_term", "term": "acme"}, False),
        ({"type": "memory_count", "customer_id": "acme", "count": 1}, True),
        ({"type": "artifact_types", "values": ["markdown"]}, True),
        ({"type": "citation_minimum", "count": 1}, False),
        ({"type": "no_cross_customer_data"}, True),
    ],
)
def test_standard_expectations_grade_real_journey_evidence(tmp_path, expectation, passed):
    scenario = _scenario([expectation])
    grade = DeterministicGrader().grade(scenario, _run(tmp_path, scenario))

    assert len(grade.findings) == 1
    assert grade.passed is passed


def test_paths_target_steps_and_execution_failure(tmp_path):
    scenario = _scenario(
        [{"type": "output_present", "path": "items.0.value"}],
        steps=[
            {
                "id": "first",
                "type": "run_skill",
                "skill": "account-brief",
                "input": {"customer_id": "acme"},
            }
        ],
    )
    run = _run(tmp_path, scenario).model_copy(
        update={"last_output": {"items": [{"value": None}, {"value": "ok"}]}}
    )
    assert not DeterministicGrader().grade(scenario, run).passed
    second = scenario.model_copy(
        update={
            "expectations": (scenario.expectations[0].model_copy(update={"path": "items.1.value"}),)
        }
    )
    assert DeterministicGrader().grade(second, run).passed

    expected = _scenario(
        [{"type": "output_equals", "step_id": "first", "path": "value", "value": "first"}],
        steps=[
            {
                "id": "first",
                "type": "run_skill",
                "skill": "account-brief",
                "input": {"customer_id": "acme"},
            }
        ],
    )
    step = run.steps[0].model_copy(update={"output": {"value": "first"}})
    target = run.model_copy(update={"steps": (step,), "last_output": {"value": "last"}})
    assert DeterministicGrader().grade(expected, target).passed
    failed = DeterministicGrader().grade(expected, target.model_copy(update={"success": False}))
    assert [item.code for item in failed.findings] == ["output_equals", "execution"]


def test_identity_secret_and_non_finite_evidence_are_safe(tmp_path):
    scenario = _scenario([{"type": "output_equals", "path": "secret", "value": "expected"}])
    run = _run(tmp_path, scenario).model_copy(
        update={"last_output": {"secret": "sk-" "proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"}}
    )
    grade = DeterministicGrader().grade(scenario, run)
    assert "sk-proj" not in grade.model_dump_json()
    assert grade == DeterministicGrader().grade(scenario, run)
    with pytest.raises(ValueError, match="scenario id"):
        DeterministicGrader().grade(scenario, run.model_copy(update={"scenario_id": "other"}))
    with pytest.raises(ValueError, match="non-finite"):
        DeterministicGrader().grade(
            scenario, run.model_copy(update={"last_output": {"n": float("nan")}})
        )
    with pytest.raises(ValidationError):
        grade.passed = True


def test_memory_revision_and_citation_dedupe(tmp_path):
    scenario = _scenario([{"type": "citation_minimum", "count": 3}])
    run = _run(tmp_path, scenario).model_copy(
        update={
            "last_output": {
                "citations": [
                    {"memory_record_id": "m1", "sources": ["s1", "s1"], "excerpt": "quote"},
                    {"memory_record_id": "m1", "sources": ["s1"], "excerpt": "quote"},
                ]
            }
        }
    )
    assert DeterministicGrader().grade(scenario, run).passed
    four = scenario.model_copy(
        update={"expectations": (scenario.expectations[0].model_copy(update={"count": 4}),)}
    )
    assert not DeterministicGrader().grade(four, run).passed

    scenario = _scenario(
        [{"type": "memory_revision", "customer_id": "acme", "logical_key": "risk", "revision": 3}],
        steps=[{"id": "tick", "type": "advance_time", "seconds": 1}],
    )
    run = _run(tmp_path / "revision", scenario)
    final = run.final_snapshot.model_copy(
        update={
            "memory": (
                {"customer_id": "acme", "logical_key": "risk", "revision": 1},
                {"customer_id": "acme", "logical_key": "risk", "revision": 3},
            )
        }
    )
    assert (
        DeterministicGrader()
        .grade(scenario, run.model_copy(update={"final_snapshot": final}))
        .passed
    )


def test_cross_customer_and_partial_effects_detect_mutated_evidence(tmp_path):
    scenario = _scenario(
        [{"type": "no_cross_customer_data", "step_id": "brief"}], customers=("acme", "globex")
    )
    run = _run(tmp_path, scenario)
    safe = run.steps[0].model_copy(update={"output": {"note": "acmeology"}})
    leaking = run.steps[0].model_copy(update={"output": {"customer_id": "globex"}})
    assert DeterministicGrader().grade(scenario, run.model_copy(update={"steps": (safe,)})).passed
    assert (
        not DeterministicGrader()
        .grade(scenario, run.model_copy(update={"steps": (leaking,)}))
        .passed
    )

    single = _scenario([{"type": "no_cross_customer_data", "step_id": "brief"}])
    single_run = _run(tmp_path / "single", single)
    foreign = single_run.steps[0].model_copy(update={"output": {"customer_id": "globex"}})
    assert (
        not DeterministicGrader()
        .grade(single, single_run.model_copy(update={"steps": (foreign,)}))
        .passed
    )

    scenario = _scenario(
        [{"type": "no_partial_effects", "step_id": "brief"}],
        steps=[
            {
                "id": "brief",
                "type": "run_skill",
                "skill": "missing-skill",
                "input": {"customer_id": "acme"},
                "expect_error": "SkillNotFoundError",
            }
        ],
    )
    run = _run(tmp_path / "rollback", scenario)
    assert DeterministicGrader().grade(scenario, run).passed
    after = run.steps[0].after.model_copy(update={"artifacts": ({"filename": "changed"},)})
    changed = run.steps[0].model_copy(update={"after": after})
    assert (
        not DeterministicGrader()
        .grade(scenario, run.model_copy(update={"steps": (changed,)}))
        .passed
    )


@pytest.mark.parametrize(
    "artifact",
    [
        {
            "type": "markdown",
            "filename": "../bad.md",
            "media_type": "text/markdown",
            "content": "eA==",
        },
        {
            "type": "markdown",
            "filename": "bad.txt",
            "media_type": "text/markdown",
            "content": "eA==",
        },
        {"type": "markdown", "filename": "good.md", "media_type": "text/plain", "content": "eA=="},
        {
            "type": "markdown",
            "filename": "good.md",
            "media_type": "text/markdown",
            "content": "%%%",
        },
    ],
)
def test_artifact_contract_rejects_integrity_tampering(tmp_path, artifact):
    scenario = _scenario([{"type": "artifact_types", "values": ["markdown"]}])
    run = _run(tmp_path, scenario).model_copy(update={"artifacts": (artifact,)})
    assert not DeterministicGrader().grade(scenario, run).passed


def test_no_partial_effects_rejects_a_successful_target(tmp_path):
    scenario = _scenario([{"type": "no_partial_effects", "step_id": "brief"}])

    assert not DeterministicGrader().grade(scenario, _run(tmp_path, scenario)).passed


def test_grade_models_are_strict_frozen_and_json_safe(tmp_path):
    scenario = _scenario([{"type": "forbidden_term", "term": "globex"}])
    grade = DeterministicGrader().grade(scenario, _run(tmp_path, scenario))

    with pytest.raises(ValidationError):
        type(grade.findings[0]).model_validate({"code": "x", "passed": "yes", "message": "x"})
    with pytest.raises(ValidationError):
        grade.findings[0].message = "changed"
    assert type(grade).model_validate_json(grade.model_dump_json()) == grade
