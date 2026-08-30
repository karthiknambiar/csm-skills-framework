"""Contract tests for the versioned simulation scenario DSL."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from csaf.simulations import (
    AdvanceTimeStep,
    ArtifactTypesExpectation,
    CitationMinimumExpectation,
    ClearFaultsStep,
    ForbiddenTermExpectation,
    IngestFixtureStep,
    MemoryCountExpectation,
    MemoryRevisionExpectation,
    NoCrossCustomerDataExpectation,
    NoPartialEffectsExpectation,
    OutputEqualsExpectation,
    OutputPresentExpectation,
    RunSkillStep,
    SeedMemoryStep,
    SetFaultStep,
    SimulationScenario,
)


def valid_scenario() -> dict[str, object]:
    """Return a fresh minimal valid scenario payload."""

    return {
        "schema_version": 1,
        "id": "sparse-account",
        "title": "Sparse account",
        "seed": 7,
        "customers": ["acme"],
        "steps": [
            {
                "id": "seed",
                "type": "seed_memory",
                "records": [
                    {
                        "customer_id": "acme",
                        "kind": "profile",
                        "content": "ACME is an enterprise customer.",
                    }
                ],
            },
            {
                "id": "brief",
                "type": "run_skill",
                "skill": "account-brief",
                "input": {"customer_id": "acme", "include_risks": True},
            },
        ],
        "expectations": [
            {
                "type": "output_equals",
                "step_id": "brief",
                "path": "executive_summary",
                "value": "x",
            }
        ],
    }


def test_scenario_validates_typed_steps_and_expectations() -> None:
    scenario = SimulationScenario.model_validate(valid_scenario())

    assert scenario.schema_version == 1
    assert isinstance(scenario.steps[0], SeedMemoryStep)
    assert isinstance(scenario.steps[1], RunSkillStep)
    assert scenario.steps[1].type == "run_skill"
    assert isinstance(scenario.expectations[0], OutputEqualsExpectation)
    assert scenario.steps[0].records[0].customer_id == "acme"


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"type": "seed_memory", "records": []}, SeedMemoryStep),
        (
            {"type": "run_skill", "skill": "qbr", "input": {}, "expect_error": "boom"},
            RunSkillStep,
        ),
        ({"type": "advance_time", "seconds": 1}, AdvanceTimeStep),
        ({"type": "set_fault", "fault": "office_missing"}, SetFaultStep),
        ({"type": "clear_faults"}, ClearFaultsStep),
        (
            {"type": "ingest_fixture", "customer_id": "acme", "fixture": "sparse.json"},
            IngestFixtureStep,
        ),
    ],
)
def test_every_step_discriminator_parses_specific_model(
    payload: dict[str, object], expected_type: type[object]
) -> None:
    data = valid_scenario()
    data["steps"] = [payload]

    scenario = SimulationScenario.model_validate(data)

    assert isinstance(scenario.steps[0], expected_type)


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"type": "output_equals", "path": "summary", "value": None}, OutputEqualsExpectation),
        ({"type": "output_present", "path": "summary"}, OutputPresentExpectation),
        ({"type": "forbidden_term", "term": "guaranteed"}, ForbiddenTermExpectation),
        (
            {"type": "memory_count", "customer_id": "acme", "count": 0},
            MemoryCountExpectation,
        ),
        (
            {
                "type": "memory_revision",
                "customer_id": "acme",
                "logical_key": "renewal-date",
                "revision": 1,
            },
            MemoryRevisionExpectation,
        ),
        ({"type": "artifact_types", "values": ["powerpoint"]}, ArtifactTypesExpectation),
        ({"type": "citation_minimum", "count": 1}, CitationMinimumExpectation),
        ({"type": "no_cross_customer_data"}, NoCrossCustomerDataExpectation),
        (
            {"type": "no_partial_effects", "step_id": "failed-write"},
            NoPartialEffectsExpectation,
        ),
    ],
)
def test_every_expectation_discriminator_parses_specific_model(
    payload: dict[str, object], expected_type: type[object]
) -> None:
    data = valid_scenario()
    data["expectations"] = [payload]

    scenario = SimulationScenario.model_validate(data)

    assert isinstance(scenario.expectations[0], expected_type)


@pytest.mark.parametrize(
    ("location", "unknown_payload"),
    [
        ("scenario", {"unexpected": True}),
        ("step", {"unexpected": True}),
        ("expectation", {"unexpected": True}),
    ],
)
def test_unknown_fields_are_rejected(location: str, unknown_payload: dict[str, object]) -> None:
    data = valid_scenario()
    if location == "scenario":
        data.update(unknown_payload)
    elif location == "step":
        assert isinstance(data["steps"], list)
        data["steps"][0].update(unknown_payload)
    else:
        assert isinstance(data["expectations"], list)
        data["expectations"][0].update(unknown_payload)

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


def test_duplicate_non_null_step_ids_are_rejected_but_missing_ids_may_repeat() -> None:
    duplicate = valid_scenario()
    duplicate["steps"] = [
        {"id": "same", "type": "advance_time", "seconds": 1},
        {"id": "same", "type": "clear_faults"},
    ]

    with pytest.raises(ValidationError, match="step ids must be unique"):
        SimulationScenario.model_validate(duplicate)

    no_ids = deepcopy(duplicate)
    no_ids["steps"] = [
        {"type": "advance_time", "seconds": 1},
        {"type": "clear_faults"},
    ]
    assert len(SimulationScenario.model_validate(no_ids).steps) == 2


@pytest.mark.parametrize("schema_version", [0, 2, "1", True])
def test_schema_version_must_be_exact_integer_one(schema_version: object) -> None:
    data = valid_scenario()
    data["schema_version"] = schema_version

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


@pytest.mark.parametrize(
    "scenario_id",
    ["", "Sparse-account", "sparse_account", "-sparse", "sparse-", "sparse--account"],
)
def test_scenario_id_must_be_kebab_case(scenario_id: str) -> None:
    data = valid_scenario()
    data["id"] = scenario_id

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


def test_single_segment_lowercase_scenario_id_is_valid() -> None:
    data = valid_scenario()
    data["id"] = "sparse"

    assert SimulationScenario.model_validate(data).id == "sparse"


@pytest.mark.parametrize("customers", [[], ["acme", "acme"], [""]])
def test_customers_must_be_non_empty_unique_non_empty_strings(customers: list[str]) -> None:
    data = valid_scenario()
    data["customers"] = customers

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


@pytest.mark.parametrize(("field", "value"), [("title", ""), ("steps", []), ("expectations", [])])
def test_required_scenario_collections_and_title_are_non_empty(field: str, value: object) -> None:
    data = valid_scenario()
    data[field] = value

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


@pytest.mark.parametrize("seconds", [1, 31_536_000])
def test_advance_time_accepts_inclusive_boundaries(seconds: int) -> None:
    data = valid_scenario()
    data["steps"] = [{"type": "advance_time", "seconds": seconds}]

    assert SimulationScenario.model_validate(data).steps[0].seconds == seconds


@pytest.mark.parametrize("seconds", [0, 31_536_001])
def test_advance_time_rejects_values_outside_boundaries(seconds: int) -> None:
    data = valid_scenario()
    data["steps"] = [{"type": "advance_time", "seconds": seconds}]

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


@pytest.mark.parametrize(
    "fault",
    [
        "office_missing",
        "office_render_failure",
        "artifact_commit_failure",
        "connector_timeout",
        "connector_rate_limit",
        "corrupt_template",
    ],
)
def test_set_fault_accepts_every_supported_fault_and_defaults_remaining_calls(fault: str) -> None:
    data = valid_scenario()
    data["steps"] = [{"type": "set_fault", "fault": fault}]

    step = SimulationScenario.model_validate(data).steps[0]

    assert isinstance(step, SetFaultStep)
    assert step.remaining_calls == 1


@pytest.mark.parametrize("remaining_calls", [1, 100])
def test_set_fault_accepts_remaining_call_boundaries(remaining_calls: int) -> None:
    data = valid_scenario()
    data["steps"] = [
        {"type": "set_fault", "fault": "connector_timeout", "remaining_calls": remaining_calls}
    ]

    assert SimulationScenario.model_validate(data).steps[0].remaining_calls == remaining_calls


@pytest.mark.parametrize("remaining_calls", [0, 101])
def test_set_fault_rejects_remaining_calls_outside_boundaries(remaining_calls: int) -> None:
    data = valid_scenario()
    data["steps"] = [
        {"type": "set_fault", "fault": "connector_timeout", "remaining_calls": remaining_calls}
    ]

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


def test_set_fault_rejects_unknown_fault() -> None:
    data = valid_scenario()
    data["steps"] = [{"type": "set_fault", "fault": "network_down"}]

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


@pytest.mark.parametrize(
    "step",
    [
        {"type": "run_skill", "skill": "", "input": {}},
        {"type": "ingest_fixture", "customer_id": "", "fixture": "fixture.json"},
        {"type": "ingest_fixture", "customer_id": "acme", "fixture": ""},
    ],
)
def test_step_required_strings_are_non_empty(step: dict[str, object]) -> None:
    data = valid_scenario()
    data["steps"] = [step]

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


@pytest.mark.parametrize(
    "expectation",
    [
        {"type": "output_present", "path": ""},
        {"type": "forbidden_term", "term": ""},
        {"type": "memory_count", "customer_id": "", "count": 0},
        {
            "type": "memory_revision",
            "customer_id": "acme",
            "logical_key": "",
            "revision": 1,
        },
        {"type": "output_present", "path": "summary", "step_id": ""},
        {"type": "no_partial_effects"},
        {"type": "no_partial_effects", "step_id": ""},
    ],
)
def test_expectation_required_strings_and_no_partial_step_are_non_empty(
    expectation: dict[str, object],
) -> None:
    data = valid_scenario()
    data["expectations"] = [expectation]

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


@pytest.mark.parametrize(
    "expectation",
    [
        {"type": "memory_count", "customer_id": "acme", "count": -1},
        {
            "type": "memory_revision",
            "customer_id": "acme",
            "logical_key": "risk",
            "revision": 0,
        },
        {"type": "artifact_types", "values": []},
        {"type": "citation_minimum", "count": 0},
    ],
)
def test_expectation_numeric_and_collection_lower_boundaries_are_enforced(
    expectation: dict[str, object],
) -> None:
    data = valid_scenario()
    data["expectations"] = [expectation]

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


@pytest.mark.parametrize("location", ["step", "expectation"])
def test_unknown_discriminator_is_rejected(location: str) -> None:
    data = valid_scenario()
    if location == "step":
        data["steps"] = [{"type": "teleport"}]
    else:
        data["expectations"] = [{"type": "always_pass"}]

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


def test_scenario_is_frozen_and_json_serializable_deterministically() -> None:
    scenario = SimulationScenario.model_validate(valid_scenario())

    first = json.dumps(scenario.model_dump(mode="json"), sort_keys=True)
    second = json.dumps(scenario.model_dump(mode="json"), sort_keys=True)

    assert first == second
    with pytest.raises(ValidationError):
        scenario.title = "Changed"
