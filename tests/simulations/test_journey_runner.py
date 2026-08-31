"""Execution and evidence contracts for deterministic journey simulations."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import csaf.simulations.runner as journey_runner_module
from csaf.connectors.errors import ConnectorDataError
from csaf.schemas import MemoryKind, MemoryRecordCreate
from csaf.simulations import JourneyRunner, SimulationScenario, SimulationWorld

START = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)


def _scenario(steps: list[dict[str, object]], *, seed: int = 7) -> SimulationScenario:
    return SimulationScenario.model_validate(
        {
            "schema_version": 1,
            "id": "runner-contract",
            "title": "Runner contract",
            "seed": seed,
            "customers": ["acme"],
            "steps": steps,
            "expectations": [{"type": "no_cross_customer_data"}],
        }
    )


def _record(content: str = "Renewal risk") -> dict[str, object]:
    return {
        "customer_id": "acme",
        "kind": "risk",
        "content": content,
        "logical_key": "renewal-risk",
    }


def _write_fixture(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"records": records}), encoding="utf-8")


def test_runner_executes_all_step_types_with_ordered_evidence(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    _write_fixture(
        fixture_root / "support.json",
        [{"id": "case-1", "kind": "support", "content": "Case escalated"}],
    )
    scenario = _scenario(
        [
            {"id": "seed", "type": "seed_memory", "records": [_record()]},
            {"type": "advance_time", "seconds": 60},
            {
                "type": "set_fault",
                "fault": "connector_timeout",
                "remaining_calls": 1,
            },
            {"type": "clear_faults"},
            {
                "type": "ingest_fixture",
                "customer_id": "acme",
                "fixture": "support.json",
            },
            {
                "id": "brief",
                "type": "run_skill",
                "skill": "account-brief",
                "input": {"customer_id": "acme"},
            },
        ]
    )
    world = SimulationWorld.create(tmp_path / "world", START, scenario.seed)
    try:
        result = JourneyRunner(world, fixture_root).run(scenario)

        assert result.success is True
        assert tuple(step.id for step in result.steps) == (
            "seed",
            "step-2",
            "step-3",
            "step-4",
            "step-5",
            "brief",
        )
        assert tuple(step.type for step in result.steps) == (
            "seed_memory",
            "advance_time",
            "set_fault",
            "clear_faults",
            "ingest_fixture",
            "run_skill",
        )
        assert all(step.success for step in result.steps)
        assert result.steps[0].before.memory == ()
        assert len(result.steps[0].after.memory) == 1
        assert len(result.steps[4].after.memory) == 2
        assert result.steps[5].output == result.last_output
        assert result.outputs == (result.steps[5].output,)
        assert result.started_at == START
        assert result.completed_at == START.replace(minute=31)
        json.loads(result.model_dump_json())
    finally:
        world.close()


def test_expected_error_continues_but_missing_expected_error_stops(tmp_path: Path) -> None:
    expected = _scenario(
        [
            {
                "type": "run_skill",
                "skill": "missing-skill",
                "input": {"customer_id": "acme"},
                "expect_error": "SkillNotFoundError",
            },
            {"type": "seed_memory", "records": [_record()]},
        ]
    )
    with SimulationWorld.create(tmp_path / "expected", START, expected.seed) as world:
        result = JourneyRunner(world).run(expected)
        assert result.success is True
        assert result.steps[0].success is True
        assert result.steps[0].expected_error is True
        assert result.steps[0].error_type == "SkillNotFoundError"
        assert len(result.steps) == 2

    missing = _scenario(
        [
            {
                "type": "run_skill",
                "skill": "account-brief",
                "input": {"customer_id": "acme"},
                "expect_error": "failure",
            },
            {"type": "seed_memory", "records": [_record("must not run")]},
        ]
    )
    with SimulationWorld.create(tmp_path / "missing", START, missing.seed) as world:
        result = JourneyRunner(world).run(missing)
        assert result.success is False
        assert len(result.steps) == 1
        assert result.steps[0].error_type == "ExpectedErrorNotRaised"
        assert result.final_snapshot.memory == ()


def test_unexpected_error_is_sanitized_captured_and_stops(tmp_path: Path) -> None:
    secret = "sk-" "proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    scenario = _scenario(
        [
            {
                "type": "run_skill",
                "skill": "missing-skill",
                "input": {"customer_id": "acme", "token": secret},
            },
            {"type": "seed_memory", "records": [_record("must not run")]},
        ]
    )
    with SimulationWorld.create(tmp_path / "world", START, scenario.seed) as world:
        result = JourneyRunner(world).run(scenario)
        assert result.success is False
        assert len(result.steps) == 1
        assert result.steps[0].success is False
        assert result.steps[0].before == result.steps[0].after
        dumped = result.model_dump_json()
        assert secret not in dumped
        assert "Traceback" not in dumped
        assert result.steps[0].error_type == "SkillNotFoundError"


def test_replay_is_deterministic_after_workspace_normalization(tmp_path: Path) -> None:
    scenario = _scenario(
        [
            {"type": "seed_memory", "records": [_record()]},
            {
                "type": "run_skill",
                "skill": "account-brief",
                "input": {"customer_id": "acme"},
            },
        ],
        seed=19,
    )
    with SimulationWorld.create(tmp_path / "one", START, 19) as first:
        one = JourneyRunner(first).run(scenario)
    with SimulationWorld.create(tmp_path / "two", START, 19) as second:
        two = JourneyRunner(second).run(scenario)

    assert one == two
    assert one.model_dump(mode="json") == two.model_dump(mode="json")


@pytest.mark.parametrize(
    "payload",
    [
        '{"records": [], "records": []}',
        '{"records": [}',
        "[]",
        '{"records": [{"id": "x", "kind": "risk", "content": NaN}]}',
        '{"records": [{"id": "x", "kind": "risk"}]}',
    ],
)
def test_fixture_rejects_malformed_documents_without_partial_writes(
    tmp_path: Path, payload: str
) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "bad.json").write_text(payload, encoding="utf-8")
    scenario = _scenario([{"type": "ingest_fixture", "customer_id": "acme", "fixture": "bad.json"}])
    with SimulationWorld.create(tmp_path / "world", START, scenario.seed) as world:
        result = JourneyRunner(world, fixtures).run(scenario)
        assert result.success is False
        assert result.final_snapshot.memory == ()
        assert result.steps[0].before == result.steps[0].after


@pytest.mark.parametrize("fixture", ["missing.json", "../escape.json"])
def test_fixture_rejects_missing_and_escape_paths(tmp_path: Path, fixture: str) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    scenario = _scenario([{"type": "ingest_fixture", "customer_id": "acme", "fixture": fixture}])
    with SimulationWorld.create(tmp_path / "world", START, scenario.seed) as world:
        result = JourneyRunner(world, fixtures).run(scenario)
        assert result.success is False
        assert result.final_snapshot.memory == ()
        assert str(tmp_path.resolve()) not in result.model_dump_json()


def test_fixture_rejects_symlink(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    outside = tmp_path / "outside.json"
    _write_fixture(outside, [{"id": "case-1", "kind": "support", "content": "private"}])
    link = fixtures / "link.json"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")
    scenario = _scenario(
        [{"type": "ingest_fixture", "customer_id": "acme", "fixture": "link.json"}]
    )
    with SimulationWorld.create(tmp_path / "world", START, scenario.seed) as world:
        result = JourneyRunner(world, fixtures).run(scenario)
        assert result.success is False
        assert result.final_snapshot.memory == ()


@pytest.mark.parametrize(
    ("fault", "error_text"),
    [("connector_timeout", "timeout"), ("connector_rate_limit", "rate limit")],
)
def test_connector_fault_has_no_partial_append_and_recovery_ingests_once(
    tmp_path: Path, fault: str, error_text: str
) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _write_fixture(
        fixtures / "support.json",
        [{"id": "case-1", "kind": "support", "content": "Case escalated"}],
    )
    failing = _scenario(
        [
            {"type": "set_fault", "fault": fault},
            {
                "type": "ingest_fixture",
                "customer_id": "acme",
                "fixture": "support.json",
            },
        ]
    )
    with SimulationWorld.create(tmp_path / "world", START, failing.seed) as world:
        first = JourneyRunner(world, fixtures).run(failing)
        assert first.success is False
        assert first.steps[1].error_type == "ConnectorError"
        assert error_text in (first.steps[1].error_message or "")
        assert first.steps[1].before.memory == first.steps[1].after.memory == ()

        recovery = JourneyRunner(world, fixtures).run(
            _scenario(
                [
                    {
                        "type": "ingest_fixture",
                        "customer_id": "acme",
                        "fixture": "support.json",
                    }
                ]
            )
        )
        assert recovery.success is True
        assert len(recovery.final_snapshot.memory) == 1


def test_runner_does_not_close_caller_owned_world(tmp_path: Path) -> None:
    scenario = _scenario([{"type": "advance_time", "seconds": 1}])
    world = SimulationWorld.create(tmp_path / "world", START, scenario.seed)
    try:
        JourneyRunner(world).run(scenario)
        persisted = world.seed((MemoryRecordCreate(**_record("still open")),))
        assert persisted[0].kind is MemoryKind.RISK
    finally:
        world.close()


def test_runner_retains_initial_snapshot_and_redacts_validation_values(tmp_path: Path) -> None:
    secret = "hunter2-super-private-value"
    scenario = _scenario(
        [
            {
                "type": "run_skill",
                "skill": "account-brief",
                "input": {"customer_id": "acme", "password": secret},
            }
        ]
    )
    with SimulationWorld.create(tmp_path / "world", START, scenario.seed) as world:
        result = JourneyRunner(world).run(scenario)

    assert result.initial_snapshot == result.steps[0].before
    assert result.initial_snapshot.memory == ()
    dumped = result.model_dump_json()
    assert secret not in dumped
    assert "input_value" not in dumped
    assert "Traceback" not in dumped


def test_expected_runtime_error_from_artifact_fault_continues(tmp_path: Path) -> None:
    scenario = _scenario(
        [
            {"type": "set_fault", "fault": "artifact_commit_failure"},
            {
                "type": "run_skill",
                "skill": "qbr",
                "input": {"customer_id": "acme", "quarter": "2026-Q1"},
                "expect_error": "RuntimeError",
            },
            {"type": "seed_memory", "records": [_record()]},
        ]
    )
    with SimulationWorld.create(tmp_path / "world", START, scenario.seed) as world:
        result = JourneyRunner(world).run(scenario)

    assert result.success is True
    assert result.steps[1].success is True
    assert result.steps[1].expected_error is True
    assert result.steps[1].error_type == "RuntimeError"
    assert len(result.steps) == 3


def test_expected_error_cannot_match_generic_safe_placeholder(tmp_path: Path) -> None:
    secret = "sk-" "proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    scenario = _scenario(
        [
            {
                "type": "run_skill",
                "skill": "missing-skill",
                "input": {"customer_id": "acme", "token": secret},
                "expect_error": "skill operation failed",
            },
            {"type": "seed_memory", "records": [_record("must not run")]},
        ]
    )
    with SimulationWorld.create(tmp_path / "world", START, scenario.seed) as world:
        result = JourneyRunner(world).run(scenario)

    assert result.success is False
    assert len(result.steps) == 1
    assert result.steps[0].error_type == "SkillNotFoundError"
    assert secret not in result.model_dump_json()


def test_mismatched_expected_qbr_error_restores_exact_pre_step_effects(tmp_path: Path) -> None:
    world = SimulationWorld.create(tmp_path / "world", START, 7)
    database = world.database_path
    try:
        artifacts = world.workspace / "artifacts"
        artifacts.mkdir()
        existing = artifacts / "acme-2026-Q1-qbr-v1.pptx"
        existing.write_bytes(b"original-presentation")
        before_files = {path.name: path.read_bytes() for path in artifacts.iterdir()}
        before_requests = world.office.requests
        result = JourneyRunner(world).run(
            _scenario(
                [
                    {
                        "type": "run_skill",
                        "skill": "qbr",
                        "input": {"customer_id": "acme", "quarter": "2026-Q1"},
                        "expect_error": "NotTheRaisedError",
                    }
                ]
            )
        )

        assert result.success is False
        assert result.steps[0].before == result.steps[0].after
        assert {path.name: path.read_bytes() for path in artifacts.iterdir()} == before_files
        assert world.office.requests == before_requests
        assert result.final_snapshot.memory == ()
    finally:
        world.close()
    renamed = tmp_path / "renamed-after-rollback.sqlite3"
    database.rename(renamed)
    renamed.unlink()


def test_fixture_reader_rejects_identity_change_without_path_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "fixture.json"
    _write_fixture(fixture, [])
    original_lstat = os.lstat
    calls = 0

    def changing_lstat(path: str | os.PathLike[str]) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = original_lstat(path)
        if calls == 2:
            values = list(result)
            values[1] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(journey_runner_module.os, "lstat", changing_lstat)
    with pytest.raises(ConnectorDataError, match="identity") as caught:
        journey_runner_module._read_fixture_bytes(fixture)

    assert str(fixture) not in str(caught.value)


def test_mismatched_expected_error_restores_deterministic_id_sequence(tmp_path: Path) -> None:
    mismatch = _scenario(
        [
            {
                "type": "run_skill",
                "skill": "account-brief",
                "input": {"customer_id": "acme"},
                "expect_error": "NotTheRaisedError",
            }
        ],
        seed=23,
    )
    normal = _scenario(
        [
            {
                "type": "run_skill",
                "skill": "account-brief",
                "input": {"customer_id": "acme"},
            }
        ],
        seed=23,
    )
    with SimulationWorld.create(tmp_path / "reused", START, 23) as reused:
        assert JourneyRunner(reused).run(mismatch).success is False
        resumed = JourneyRunner(reused).run(normal)
    with SimulationWorld.create(tmp_path / "fresh", START, 23) as fresh:
        baseline = JourneyRunner(fresh).run(normal)

    assert resumed == baseline


def test_committed_expected_error_does_not_rewind_deterministic_id_sequence(
    tmp_path: Path,
) -> None:
    expected_failure = _scenario(
        [
            {"type": "set_fault", "fault": "artifact_commit_failure"},
            {
                "type": "run_skill",
                "skill": "qbr",
                "input": {"customer_id": "acme", "quarter": "2026-Q1"},
                "expect_error": "RuntimeError",
            },
        ],
        seed=29,
    )
    with SimulationWorld.create(tmp_path / "reused", START, 29) as reused:
        assert JourneyRunner(reused).run(expected_failure).success is True
        resumed = reused.runtime.runner.run("account-brief", {"customer_id": "acme"})
    with SimulationWorld.create(tmp_path / "fresh", START, 29) as fresh:

        def fail_artifact(_: object) -> None:
            raise RuntimeError("simulated artifact commit failure")

        with pytest.raises(RuntimeError, match="simulated artifact commit failure"):
            fresh.runtime.runner.run(
                "qbr",
                {"customer_id": "acme", "quarter": "2026-Q1"},
                artifact_handler=fail_artifact,
            )
        baseline = fresh.runtime.runner.run("account-brief", {"customer_id": "acme"})

    assert resumed.execution_id == baseline.execution_id
