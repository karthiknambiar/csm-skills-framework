"""Dataset loading tests for deterministic simulation scenarios."""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import pytest
from pydantic import ValidationError

from csaf.simulations import SimulationDatasetError, SimulationScenario, load_scenarios


def scenario_payload(
    scenario_id: str = "valid-scenario",
    *,
    fixture: str | None = None,
) -> dict[str, object]:
    """Return a fresh minimal valid scenario payload."""

    step: dict[str, object]
    if fixture is None:
        step = {"type": "advance_time", "seconds": 1}
    else:
        step = {
            "type": "ingest_fixture",
            "customer_id": "acme",
            "fixture": fixture,
        }
    return {
        "schema_version": 1,
        "id": scenario_id,
        "title": "Valid scenario",
        "seed": 7,
        "customers": ["acme"],
        "steps": [step],
        "expectations": [{"type": "no_cross_customer_data"}],
    }


def write_scenario(path: Path, scenario_id: str, *, fixture: str | None = None) -> None:
    path.write_text(json.dumps(scenario_payload(scenario_id, fixture=fixture)), encoding="utf-8")


def assert_traceback_and_cause_chain_hide(error: BaseException, secret: str) -> None:
    """Assert public diagnostics and every explicit cause omit source content."""

    assert secret not in "".join(traceback.format_exception(error))
    assert error.__context__ is None
    cause = error.__cause__
    assert cause is not None
    while cause is not None:
        cause_text = f"{cause!s}\n{cause!r}\n{vars(cause)!r}"
        assert secret not in cause_text
        assert cause.__context__ is None
        cause = cause.__cause__


def test_loads_one_json_file_as_an_immutable_tuple(tmp_path: Path) -> None:
    source = tmp_path / "one.json"
    write_scenario(source, "one")

    scenarios = load_scenarios(source)

    assert isinstance(scenarios, tuple)
    assert len(scenarios) == 1
    assert isinstance(scenarios[0], SimulationScenario)
    assert scenarios[0].id == "one"
    with pytest.raises(ValidationError):
        scenarios[0].title = "Changed"


def test_directory_load_is_non_recursive_json_only_and_sorted_by_path(tmp_path: Path) -> None:
    write_scenario(tmp_path / "z-last.json", "z-last")
    write_scenario(tmp_path / "a-first.json", "a-first")
    (tmp_path / "ignored.txt").write_text("not JSON", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    write_scenario(nested / "hidden.json", "hidden")

    scenarios = load_scenarios(tmp_path)

    assert tuple(scenario.id for scenario in scenarios) == ("a-first", "z-last")


@pytest.mark.parametrize("source_kind", ["missing", "non-json-file"])
def test_rejects_missing_or_unsupported_input_paths(
    tmp_path: Path, source_kind: str
) -> None:
    source = tmp_path / "missing.json"
    if source_kind == "non-json-file":
        source = tmp_path / "scenario.txt"
        source.write_text("{}", encoding="utf-8")

    with pytest.raises(SimulationDatasetError, match=source.name) as caught:
        load_scenarios(source)

    assert caught.value.__cause__ is not None


def test_rejects_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(SimulationDatasetError) as caught:
        load_scenarios(tmp_path)

    assert str(tmp_path) in str(caught.value)
    assert "no JSON scenario files" in str(caught.value)
    assert caught.value.__cause__ is not None


def test_rejects_non_utf8_without_leaking_file_contents(tmp_path: Path) -> None:
    source = tmp_path / "secret.json"
    source.write_bytes(b'{"title":"private-\xff-value"}')

    with pytest.raises(SimulationDatasetError, match=source.name) as caught:
        load_scenarios(source)

    assert_traceback_and_cause_chain_hide(caught.value, "private")
    assert type(caught.value.__cause__) is ValueError


def test_rejects_malformed_json_without_leaking_file_contents(tmp_path: Path) -> None:
    source = tmp_path / "broken.json"
    source.write_text('{"secret-value":', encoding="utf-8")

    with pytest.raises(SimulationDatasetError, match=source.name) as caught:
        load_scenarios(source)

    assert_traceback_and_cause_chain_hide(caught.value, "secret-value")
    assert type(caught.value.__cause__) is ValueError


def test_wraps_json_integer_digit_limit_errors(tmp_path: Path) -> None:
    source = tmp_path / "huge-number.json"
    encoded = json.dumps(scenario_payload()).replace('"seed": 7', '"seed": ' + "9" * 5000)
    source.write_text(encoded, encoding="utf-8")

    with pytest.raises(SimulationDatasetError, match=source.name) as caught:
        load_scenarios(source)

    assert "JSON" in str(caught.value)
    assert_traceback_and_cause_chain_hide(caught.value, "9" * 50)
    assert type(caught.value.__cause__) is ValueError


@pytest.mark.parametrize("location", ["root", "nested"])
def test_rejects_duplicate_json_keys_without_echoing_the_key(
    tmp_path: Path, location: str
) -> None:
    source = tmp_path / f"duplicate-{location}.json"
    secret_key = "secret-key-do-not-leak"
    if location == "root":
        encoded = json.dumps(scenario_payload())[:-1]
        encoded += f', "{secret_key}": 1, "{secret_key}": 2}}'
    else:
        payload = scenario_payload()
        step = (
            '{"type":"run_skill","skill":"brief","input":'
            f'{{"customer_id":"acme","{secret_key}":1,"{secret_key}":2}}}}'
        )
        encoded = json.dumps(payload).replace(
            '{"type": "advance_time", "seconds": 1}', step
        )
    source.write_text(encoded, encoding="utf-8")

    with pytest.raises(SimulationDatasetError, match=source.name) as caught:
        load_scenarios(source)

    assert "duplicate JSON object key" in str(caught.value)
    assert_traceback_and_cause_chain_hide(caught.value, secret_key)
    assert type(caught.value.__cause__) is ValueError


@pytest.mark.parametrize("root", [[], [scenario_payload()], None, "scenario"])
def test_rejects_non_object_json_roots(tmp_path: Path, root: object) -> None:
    source = tmp_path / "root.json"
    source.write_text(json.dumps(root), encoding="utf-8")

    with pytest.raises(SimulationDatasetError, match=source.name) as caught:
        load_scenarios(source)

    assert "JSON object" in str(caught.value)
    assert caught.value.__cause__ is not None


@pytest.mark.parametrize(
    ("field", "value", "cause_text"),
    [
        ("customers", [], "customers"),
        ("schema_version", 2, "schema_version"),
    ],
)
def test_wraps_schema_validation_errors(
    tmp_path: Path, field: str, value: object, cause_text: str
) -> None:
    source = tmp_path / "invalid.json"
    payload = scenario_payload()
    payload[field] = value
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SimulationDatasetError, match=source.name) as caught:
        load_scenarios(source)

    assert cause_text in str(caught.value)
    assert type(caught.value.__cause__) is ValueError


def test_schema_validation_error_does_not_leak_invalid_discriminator(tmp_path: Path) -> None:
    source = tmp_path / "invalid-step.json"
    payload = scenario_payload()
    payload["steps"] = [{"type": "secret-do-not-leak"}]
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SimulationDatasetError) as caught:
        load_scenarios(source)

    message = str(caught.value)
    assert source.name in message
    assert "steps.0" in message
    assert "union_tag_invalid" in message
    assert "secret-do-not-leak" not in message
    assert_traceback_and_cause_chain_hide(caught.value, "secret-do-not-leak")
    assert type(caught.value.__cause__) is ValueError


def test_rejects_duplicate_ids_across_files(tmp_path: Path) -> None:
    first = tmp_path / "a.json"
    duplicate = tmp_path / "b.json"
    write_scenario(first, "duplicate")
    write_scenario(duplicate, "duplicate")

    with pytest.raises(SimulationDatasetError, match=duplicate.name) as caught:
        load_scenarios(tmp_path)

    assert "duplicate" in str(caught.value)
    assert first.name in str(caught.value)
    assert caught.value.__cause__ is not None


@pytest.mark.parametrize(
    "fixture",
    [
        "/fixtures/account.json",
        "C:/fixtures/account.json",
        r"C:\fixtures\account.json",
        r"C:fixture.json",
        r"\fixture.json",
        r"\\server\share\account.json",
        "../account.json",
        "fixtures/../account.json",
        r"..\account.json",
        r"fixtures\..\account.json",
    ],
)
def test_rejects_fixture_paths_outside_the_fixture_boundary(
    tmp_path: Path, fixture: str
) -> None:
    source = tmp_path / "unsafe.json"
    write_scenario(source, "unsafe", fixture=fixture)

    with pytest.raises(SimulationDatasetError, match=source.name) as caught:
        load_scenarios(source)

    assert fixture in str(caught.value)
    assert caught.value.__cause__ is not None


@pytest.mark.parametrize("fixture", ["account.json", "nested/account.json", r"nested\account.json"])
def test_allows_nested_relative_fixture_paths(tmp_path: Path, fixture: str) -> None:
    source = tmp_path / "safe.json"
    write_scenario(source, "safe", fixture=fixture)

    assert load_scenarios(source)[0].steps[0].fixture == fixture


def test_invalid_sibling_prevents_partial_success(tmp_path: Path) -> None:
    write_scenario(tmp_path / "a-valid.json", "valid")
    malformed = tmp_path / "z-malformed.json"
    malformed.write_text("{", encoding="utf-8")

    with pytest.raises(SimulationDatasetError, match=malformed.name):
        load_scenarios(tmp_path)
