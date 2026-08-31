"""Contract tests for the versioned simulation scenario DSL."""

import json
import warnings
from copy import copy, deepcopy
from typing import get_type_hints

import pytest
from pydantic import ValidationError

import csaf.simulations as simulations
from csaf.schemas import MemoryRecordCreate
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
    "step",
    [
        {"id": "", "type": "seed_memory", "records": []},
        {"id": "", "type": "run_skill", "skill": "qbr", "input": {}},
        {"id": "", "type": "advance_time", "seconds": 1},
        {"id": "", "type": "set_fault", "fault": "office_missing"},
        {"id": "", "type": "clear_faults"},
        {"id": "", "type": "ingest_fixture", "customer_id": "acme", "fixture": "a.json"},
    ],
)
def test_step_ids_reject_empty_strings(step: dict[str, object]) -> None:
    data = valid_scenario()
    data["steps"] = [step]

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


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
            {"type": "no_partial_effects", "step_id": "brief"},
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


def test_nested_json_values_and_seed_records_are_deeply_immutable() -> None:
    data = valid_scenario()
    data["steps"][0]["records"][0]["metadata"] = {
        "labels": ["enterprise"],
        "details": {"tier": "one"},
    }
    data["steps"][1]["input"] = {
        "customer_id": "acme",
        "filters": {"kinds": ["risk"], "options": {"latest": True}},
    }
    data["expectations"][0]["value"] = {"sections": [{"name": "summary"}]}
    scenario = SimulationScenario.model_validate(data)

    seed_step = scenario.steps[0]
    run_step = scenario.steps[1]
    expectation = scenario.expectations[0]
    assert isinstance(seed_step, SeedMemoryStep)
    assert isinstance(run_step, RunSkillStep)
    assert isinstance(expectation, OutputEqualsExpectation)

    record = seed_step.records[0]
    assert isinstance(record, MemoryRecordCreate)
    with pytest.raises(ValidationError):
        record.content = "Changed"
    details = record.metadata["details"]
    labels = record.metadata["labels"]
    assert isinstance(details, dict)
    assert isinstance(labels, tuple)
    with pytest.raises(TypeError):
        details["tier"] = "two"
    with pytest.raises(TypeError):
        labels[0] = "changed"

    filters = run_step.input["filters"]
    assert isinstance(filters, dict)
    kinds = filters["kinds"]
    assert isinstance(kinds, tuple)
    with pytest.raises(TypeError):
        filters["new"] = True
    with pytest.raises(TypeError):
        kinds[0] = "support"

    value = expectation.value
    assert isinstance(value, dict)
    sections = value["sections"]
    assert isinstance(sections, tuple)
    section = sections[0]
    assert isinstance(section, dict)
    with pytest.raises(TypeError):
        section["name"] = "changed"


def test_seed_step_copies_domain_memory_records_into_frozen_records() -> None:
    domain_record = MemoryRecordCreate(
        customer_id="acme",
        kind="profile",
        content="Domain record",
        metadata={"labels": ["enterprise"]},
    )

    step = SeedMemoryStep(type="seed_memory", records=(domain_record,))

    assert isinstance(step.records[0], MemoryRecordCreate)
    assert step.records[0] is not domain_record
    with pytest.raises(ValidationError):
        step.records[0].content = "Changed"


def test_seed_memory_public_contract_remains_domain_record_type() -> None:
    annotation = get_type_hints(SeedMemoryStep)["records"]
    schema = SeedMemoryStep.model_json_schema(mode="validation")

    assert annotation == tuple[MemoryRecordCreate, ...]
    assert schema["properties"]["records"]["items"]["$ref"] == "#/$defs/MemoryRecordCreate"
    assert "MemoryRecordCreate" in schema["$defs"]
    assert "SimulationMemoryRecordCreate" not in schema["$defs"]
    assert "SimulationMemoryRecordCreate" not in simulations.__all__
    assert not hasattr(simulations, "SimulationMemoryRecordCreate")


def test_standard_json_serialization_is_warning_free_and_round_trips() -> None:
    data = valid_scenario()
    data["steps"][0]["records"][0]["metadata"] = {
        "labels": ["enterprise"],
        "details": {"score": 1.25},
    }
    scenario = SimulationScenario.model_validate(data)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        encoded = scenario.model_dump_json()
    restored = SimulationScenario.model_validate_json(encoded)

    assert restored == scenario


def test_deep_model_copy_reuses_only_recursively_immutable_json_containers() -> None:
    data = valid_scenario()
    data["steps"][1]["input"] = {
        "customer_id": "acme",
        "nested": {"values": [1, 2]},
    }
    scenario = SimulationScenario.model_validate(data)

    copied = scenario.model_copy(deep=True)
    original_step = scenario.steps[1]
    copied_step = copied.steps[1]
    assert isinstance(original_step, RunSkillStep)
    assert isinstance(copied_step, RunSkillStep)

    assert copied == scenario
    assert copied is not scenario
    assert copy(original_step.input) is original_step.input
    assert deepcopy(original_step.input) is original_step.input
    assert copied_step.input is original_step.input
    with pytest.raises(TypeError):
        copied_step.input["new"] = True


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("location", ["input", "expectation", "metadata"])
def test_nested_json_rejects_non_finite_floats(location: str, non_finite: float) -> None:
    data = valid_scenario()
    if location == "input":
        data["steps"][1]["input"] = {
            "customer_id": "acme",
            "nested": {"values": [non_finite]},
        }
    elif location == "expectation":
        data["expectations"][0]["value"] = {"nested": [non_finite]}
    else:
        data["steps"][0]["records"][0]["metadata"] = {"nested": [non_finite]}

    with pytest.raises(ValidationError, match="JSON floats must be finite"):
        SimulationScenario.model_validate(data)


def test_finite_floats_round_trip_losslessly_at_every_json_boundary() -> None:
    data = valid_scenario()
    data["steps"][0]["records"][0]["metadata"] = {"score": 1.25}
    data["steps"][1]["input"] = {"customer_id": "acme", "score": 2.5}
    data["expectations"][0]["value"] = {"score": 3.75}
    scenario = SimulationScenario.model_validate(data)

    restored = SimulationScenario.model_validate_json(scenario.model_dump_json())

    assert restored == scenario
    assert restored.model_dump(mode="json") == scenario.model_dump(mode="json")


@pytest.mark.parametrize(
    "expectation",
    [
        {"type": "output_present", "path": "summary", "step_id": "missing"},
        {"type": "no_partial_effects", "step_id": "missing"},
    ],
)
def test_expectation_step_references_must_resolve(expectation: dict[str, object]) -> None:
    data = valid_scenario()
    data["expectations"] = [expectation]

    with pytest.raises(
        ValidationError, match="expectation step_id must reference an explicit step"
    ):
        SimulationScenario.model_validate(data)


@pytest.mark.parametrize(
    ("step", "expectation"),
    [
        (
            {
                "type": "seed_memory",
                "records": [
                    {"customer_id": "other", "kind": "profile", "content": "Other"}
                ],
            },
            None,
        ),
        ({"type": "ingest_fixture", "customer_id": "other", "fixture": "a.json"}, None),
        (
            {"type": "run_skill", "skill": "qbr", "input": {"customer_id": "other"}},
            None,
        ),
        (None, {"type": "memory_count", "customer_id": "other", "count": 0}),
        (
            None,
            {
                "type": "memory_revision",
                "customer_id": "other",
                "logical_key": "risk",
                "revision": 1,
            },
        ),
    ],
)
def test_customer_references_must_belong_to_scenario(
    step: dict[str, object] | None, expectation: dict[str, object] | None
) -> None:
    data = valid_scenario()
    if step is not None:
        data["steps"] = [*data["steps"], step]
    if expectation is not None:
        data["expectations"] = [expectation]

    with pytest.raises(ValidationError, match="customer_id must belong to scenario customers"):
        SimulationScenario.model_validate(data)


def test_run_skill_only_interprets_exact_string_customer_id_key() -> None:
    data = valid_scenario()
    data["steps"] = [
        *data["steps"],
        {
            "type": "run_skill",
            "skill": "qbr",
            "input": {"target_customer_id": "other", "customer_id": None},
        },
    ]

    assert len(SimulationScenario.model_validate(data).steps) == 3


@pytest.mark.parametrize("invalid", [True, "1", 1.0])
@pytest.mark.parametrize(
    "target",
    ["seed", "seconds", "remaining_calls", "memory_count", "revision", "citation_count"],
)
def test_numeric_dsl_fields_reject_coercion(target: str, invalid: object) -> None:
    data = valid_scenario()
    if target == "seed":
        data["seed"] = invalid
    elif target == "seconds":
        data["steps"] = [{"type": "advance_time", "seconds": invalid}]
        data["expectations"] = [{"type": "no_cross_customer_data"}]
    elif target == "remaining_calls":
        data["steps"] = [
            {
                "type": "set_fault",
                "fault": "connector_timeout",
                "remaining_calls": invalid,
            }
        ]
        data["expectations"] = [{"type": "no_cross_customer_data"}]
    elif target == "memory_count":
        data["expectations"] = [
            {"type": "memory_count", "customer_id": "acme", "count": invalid}
        ]
    elif target == "revision":
        data["expectations"] = [
            {
                "type": "memory_revision",
                "customer_id": "acme",
                "logical_key": "risk",
                "revision": invalid,
            }
        ]
    else:
        data["expectations"] = [{"type": "citation_minimum", "count": invalid}]

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


@pytest.mark.parametrize(
    "target",
    ["title", "customer", "step_id", "skill", "expect_error", "fixture", "path", "artifact"],
)
def test_non_empty_strings_reject_whitespace_only_values(target: str) -> None:
    data = valid_scenario()
    if target == "title":
        data["title"] = " \t"
    elif target == "customer":
        data["customers"] = [" \t"]
    elif target == "step_id":
        data["steps"][1]["id"] = " \t"
    elif target == "skill":
        data["steps"][1]["skill"] = " \t"
    elif target == "expect_error":
        data["steps"][1]["expect_error"] = " \t"
    elif target == "fixture":
        data["steps"] = [
            *data["steps"],
            {"type": "ingest_fixture", "customer_id": "acme", "fixture": " \t"},
        ]
    elif target == "path":
        data["expectations"] = [{"type": "output_present", "path": " \t"}]
    else:
        data["expectations"] = [{"type": "artifact_types", "values": [" \t"]}]

    with pytest.raises(ValidationError):
        SimulationScenario.model_validate(data)


def test_scenario_is_frozen_and_json_round_trips_without_warnings() -> None:
    data = valid_scenario()
    data["steps"][1]["input"] = {"customer_id": "acme", "nested": {"values": [1, 2]}}
    data["expectations"][0]["value"] = {"sections": ["summary"]}
    scenario = SimulationScenario.model_validate(data)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        dumped = scenario.model_dump(mode="json")
    encoded = json.dumps(dumped, sort_keys=True)
    restored = SimulationScenario.model_validate(json.loads(encoded))

    assert restored == scenario
    assert restored.model_dump(mode="json") == dumped
    with pytest.raises(ValidationError):
        scenario.title = "Changed"


def test_json_schema_preserves_discriminators_strict_shape_and_bounds() -> None:
    schema = SimulationScenario.model_json_schema(mode="validation")
    definitions = schema["$defs"]

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version",
        "id",
        "title",
        "seed",
        "customers",
        "steps",
        "expectations",
    }
    assert schema["properties"]["customers"]["minItems"] == 1
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["title"]["pattern"] == r"\S"

    step_items = schema["properties"]["steps"]["items"]
    assert step_items["discriminator"]["propertyName"] == "type"
    assert set(step_items["discriminator"]["mapping"]) == {
        "advance_time",
        "clear_faults",
        "ingest_fixture",
        "run_skill",
        "seed_memory",
        "set_fault",
    }
    expectation_items = schema["properties"]["expectations"]["items"]
    assert expectation_items["discriminator"]["propertyName"] == "type"
    assert len(expectation_items["discriminator"]["mapping"]) == 9

    for name in step_items["discriminator"]["mapping"].values():
        assert definitions[name.removeprefix("#/$defs/")]["additionalProperties"] is False
    for name in expectation_items["discriminator"]["mapping"].values():
        assert definitions[name.removeprefix("#/$defs/")]["additionalProperties"] is False
    assert definitions["MemoryRecordCreate"]["additionalProperties"] is False
    assert set(definitions["RunSkillStep"]["required"]) == {"type", "skill", "input"}
    assert set(definitions["OutputEqualsExpectation"]["required"]) == {
        "type",
        "path",
        "value",
    }
    assert {"customer_id", "kind", "content"}.issubset(
        definitions["MemoryRecordCreate"]["required"]
    )

    seconds = definitions["AdvanceTimeStep"]["properties"]["seconds"]
    assert seconds["exclusiveMinimum"] == 0
    assert seconds["maximum"] == 31_536_000
    remaining_calls = definitions["SetFaultStep"]["properties"]["remaining_calls"]
    assert remaining_calls["minimum"] == 1
    assert remaining_calls["maximum"] == 100
    assert definitions["MemoryCountExpectation"]["properties"]["count"]["minimum"] == 0
    assert definitions["MemoryRevisionExpectation"]["properties"]["revision"]["minimum"] == 1
    assert definitions["CitationMinimumExpectation"]["properties"]["count"]["minimum"] == 1
    assert definitions["ArtifactTypesExpectation"]["properties"]["values"]["minItems"] == 1
