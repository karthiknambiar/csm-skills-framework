"""Deterministic isolation contracts for simulation worlds."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

from csaf.office import (
    OfficeCLIError,
    OfficeFormat,
    OfficeOperation,
    OfficeRenderRequest,
    OfficeSection,
)
from csaf.schemas import MemoryKind, MemoryQuery, MemoryRecordCreate
from csaf.simulations import (
    FaultRegistry,
    MutableClock,
    SimulationOfficeRenderer,
    SimulationWorld,
)
from csaf.skills import Artifact, ArtifactType, SkillRunResult

START = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)


def _record(customer_id: str, content: str, *, logical_key: str | None = None):
    return MemoryRecordCreate(
        customer_id=customer_id,
        kind=MemoryKind.RISK,
        content=content,
        logical_key=logical_key,
        metadata={"z": 2, "a": [1, {"nested": True}]},
        occurred_at=START - timedelta(days=1),
    )


def _request(*, template_path: Path | None = None) -> OfficeRenderRequest:
    return OfficeRenderRequest(
        format=OfficeFormat.WORD,
        operation=OfficeOperation.CREATE,
        title="QBR",
        sections=(OfficeSection(title="Summary", bullets=("Renewal healthy",)),),
        template_path=template_path,
    )


def test_fault_registry_validates_counts_and_names() -> None:
    faults = FaultRegistry()

    for invalid in (0, 101, -1, True, 1.5, "1"):
        with pytest.raises((TypeError, ValueError)):
            faults.set("office_missing", remaining_calls=invalid)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        faults.set("unknown", remaining_calls=1)  # type: ignore[arg-type]

    faults.set("office_missing", remaining_calls=100)
    assert all(faults.consume("office_missing") for _ in range(100))
    assert faults.consume("office_missing") is False
    with pytest.raises((TypeError, ValueError)):
        faults.consume("unknown")  # type: ignore[arg-type]


def test_fault_registry_replaces_counts_and_consumes_exactly_once() -> None:
    faults = FaultRegistry()
    faults.set("office_missing", remaining_calls=3)
    faults.set("office_missing", remaining_calls=2)

    assert faults.consume("office_missing") is True
    assert faults.consume("office_missing") is True
    assert faults.consume("office_missing") is False


def test_fault_registry_faults_are_independent_and_clear_is_total() -> None:
    faults = FaultRegistry()
    faults.set("office_missing", remaining_calls=2)
    faults.set("connector_timeout", remaining_calls=1)

    assert faults.consume("office_missing") is True
    assert faults.consume("connector_timeout") is True
    assert faults.consume("office_missing") is True
    faults.set("office_render_failure", remaining_calls=1)
    faults.clear()
    assert faults.consume("office_render_failure") is False


def test_mutable_clock_requires_aware_time_and_enforces_advance_bounds() -> None:
    with pytest.raises(ValueError, match="timezone"):
        MutableClock(datetime(2026, 1, 1))

    clock = MutableClock(START)
    assert clock.now() == START
    clock.advance(1)
    assert clock.now() == START + timedelta(seconds=1)
    clock.advance(31_536_000)
    assert clock.now() == START + timedelta(seconds=31_536_001)
    for invalid in (0, -1, 31_536_001, True, 1.5):
        with pytest.raises((TypeError, ValueError)):
            clock.advance(invalid)  # type: ignore[arg-type]


def test_office_renderer_records_copy_safe_requests_and_returns_stable_bytes() -> None:
    renderer = SimulationOfficeRenderer(FaultRegistry())
    request = _request()

    assert renderer.render(request) == b"simulation:word:create:QBR"
    assert renderer.requests == (request,)
    assert renderer.requests[0] is not request
    with pytest.raises(ValidationError):
        renderer.requests[0].title = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("office_missing", "OfficeCLI executable was not found"),
        ("office_render_failure", "simulated office render failure"),
    ],
)
def test_office_faults_fail_exact_count_then_recover(fault: str, message: str) -> None:
    faults = FaultRegistry()
    renderer = SimulationOfficeRenderer(faults)
    faults.set(fault, remaining_calls=2)  # type: ignore[arg-type]

    for _ in range(2):
        with pytest.raises(OfficeCLIError, match=message):
            renderer.render(_request())
    assert renderer.render(_request()) == b"simulation:word:create:QBR"
    assert len(renderer.requests) == 3


def test_office_renderer_consumes_only_the_fault_that_fires() -> None:
    faults = FaultRegistry()
    renderer = SimulationOfficeRenderer(faults)
    faults.set("office_missing")
    faults.set("office_render_failure")

    with pytest.raises(OfficeCLIError, match="was not found"):
        renderer.render(_request())
    with pytest.raises(OfficeCLIError, match="simulated office render failure"):
        renderer.render(_request())
    assert renderer.render(_request()) == b"simulation:word:create:QBR"


def test_corrupt_template_fault_waits_for_template_request_then_recovers(tmp_path: Path) -> None:
    faults = FaultRegistry()
    renderer = SimulationOfficeRenderer(faults)
    faults.set("corrupt_template", remaining_calls=1)

    assert renderer.render(_request()) == b"simulation:word:create:QBR"
    with pytest.raises(OfficeCLIError, match="Office template"):
        renderer.render(_request(template_path=tmp_path / "template.docx"))
    assert renderer.render(_request(template_path=tmp_path / "template.docx")).startswith(
        b"simulation:"
    )


def test_world_creates_resolved_workspace_and_stable_database(tmp_path: Path) -> None:
    target = tmp_path / "parent" / ".." / "world"
    world = SimulationWorld.create(target, START, 7)
    try:
        assert world.workspace == target.resolve()
        assert world.workspace.is_dir()
        assert world.database_path == world.workspace / "simulation.sqlite3"
        assert world.database_path.is_file()
    finally:
        world.close()


def test_world_rejects_naive_start_and_workspace_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        SimulationWorld.create(tmp_path / "naive", datetime(2026, 1, 1), 1)
    workspace_file = tmp_path / "file"
    workspace_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        SimulationWorld.create(workspace_file, START, 1)


def test_world_uuid_sequence_is_exact_and_seed_dependent(tmp_path: Path) -> None:
    first = SimulationWorld.create(tmp_path / "first", START, 9)
    second = SimulationWorld.create(tmp_path / "second", START, 10)
    try:
        first_record = first.seed((_record("acme", "first"),))[0]
        second_record = second.seed((_record("acme", "first"),))[0]
        assert first_record.id == uuid5(NAMESPACE_URL, "9:0")
        assert second_record.id == uuid5(NAMESPACE_URL, "10:0")
        assert first_record.id != second_record.id
        result = first.runtime.runner.run("account-brief", {"customer_id": "acme"})
        assert result.execution_id == uuid5(NAMESPACE_URL, "9:1")
        assert result.memory_updates[0].id == uuid5(NAMESPACE_URL, "9:2")
    finally:
        first.close()
        second.close()


def test_world_seed_and_snapshot_are_complete_stable_and_immutable(tmp_path: Path) -> None:
    world = SimulationWorld.create(tmp_path, START, 2)
    try:
        created = world.seed(
            (
                _record("zeta", "second", logical_key="risk"),
                _record("acme", "first", logical_key="risk"),
                _record("acme", "revision two", logical_key="risk"),
            )
        )
        snapshot = world.memory_snapshot()
        assert tuple(item["customer_id"] for item in snapshot) == ("acme", "acme", "zeta")
        assert tuple(item["revision"] for item in snapshot[:2]) == (1, 2)
        assert {item["id"] for item in snapshot} == {str(record.id) for record in created}
        assert world.memory_snapshot() == snapshot
        with pytest.raises(TypeError):
            snapshot[0]["content"] = "changed"
        with pytest.raises(TypeError):
            snapshot[0]["metadata"]["a"] = []
    finally:
        world.close()


def test_world_is_deterministic_and_isolated(tmp_path: Path) -> None:
    first = SimulationWorld.create(tmp_path / "first", START, 9)
    second = SimulationWorld.create(tmp_path / "second", START, 9)
    try:
        first.seed((_record("acme", "renewal risk"),))
        second.seed((_record("acme", "renewal risk"),))
        one = first.runtime.runner.run("account-brief", {"customer_id": "acme"})
        two = second.runtime.runner.run("account-brief", {"customer_id": "acme"})
        canonical = first.canonical_result(one)
        assert canonical == second.canonical_result(two)
        assert first.database_path != second.database_path
        assert canonical["execution_id"] == str(uuid5(NAMESPACE_URL, "9:1"))
        assert canonical["started_at"] == START.isoformat().replace("+00:00", "Z")
        assert json.loads(json.dumps(canonical))["artifacts"][0]["filename"].endswith(".md")
        assert copy.deepcopy(canonical) is canonical
        with pytest.raises(TypeError):
            canonical["skill_name"] = "changed"
    finally:
        first.close()
        second.close()


def test_worlds_do_not_leak_memory_and_close_is_idempotent(tmp_path: Path) -> None:
    with SimulationWorld.create(tmp_path / "first", START, 1) as first:
        first.seed((_record("acme", "private"),))
        with SimulationWorld.create(tmp_path / "second", START, 1) as second:
            assert second.runtime.memory.search(MemoryQuery(customer_id="acme")) == []
    first.close()
    second.close()


def test_canonical_result_normalizes_only_workspace_paths(tmp_path: Path) -> None:
    world = SimulationWorld.create(tmp_path / "world", START, 1)
    try:
        value = {
            "inside": str(world.workspace / "artifacts" / "brief.md"),
            "outside": str(tmp_path.resolve() / "shared" / "input.json"),
            "id": str(uuid5(NAMESPACE_URL, "1:0")),
            "timestamp": START,
        }
        canonical = world.canonical_result(value)
        assert canonical["inside"] == "<workspace>/artifacts/brief.md"
        assert canonical["outside"] == value["outside"]
        assert canonical["id"] == value["id"]
        assert canonical["timestamp"] == START.isoformat()
    finally:
        world.close()


def test_canonical_result_normalizes_embedded_workspace_paths_across_worlds(
    tmp_path: Path,
) -> None:
    first = SimulationWorld.create(tmp_path / "first", START, 1)
    second = SimulationWorld.create(tmp_path / "second", START, 1)
    try:
        first_path = first.workspace / "templates" / "qbr.docx"
        second_path = second.workspace / "templates" / "qbr.docx"
        first_message = f"render failed at {first_path}; retry from unrelated/path"
        second_message = (
            f"render failed at {str(second_path).replace(chr(92), '/')}; retry from unrelated/path"
        )

        first_result = first.canonical_result({"error": first_message})
        second_result = second.canonical_result({"error": second_message})
        assert first_result == second_result
        assert first_result["error"] == (
            "render failed at <workspace>/templates/qbr.docx; retry from unrelated/path"
        )
    finally:
        first.close()
        second.close()


def test_canonical_result_normalizes_spaced_workspace_path_suffixes_only(
    tmp_path: Path,
) -> None:
    first = SimulationWorld.create(tmp_path / "first", START, 1)
    second = SimulationWorld.create(tmp_path / "second", START, 1)
    try:
        first_path = first.workspace / "QBR Templates" / "file.docx"
        second_path = str(second.workspace / "QBR Templates" / "file.docx").replace(chr(92), "/")
        untouched = r"raw\QBR Templates\file.docx"

        first_result = first.canonical_result(
            {"message": f"failed at {first_path}; retry", "untouched": untouched}
        )
        second_result = second.canonical_result(
            {"message": f"failed at {second_path}; retry", "untouched": untouched}
        )

        assert first_result == second_result
        assert first_result["message"] == ("failed at <workspace>/QBR Templates/file.docx; retry")
        assert first_result["untouched"] == untouched
    finally:
        first.close()
        second.close()


def test_write_artifacts_is_deterministic_and_confined_to_workspace(tmp_path: Path) -> None:
    world = SimulationWorld.create(tmp_path / "world", START, 1)
    try:
        result = SkillRunResult.model_construct(
            artifacts=(
                Artifact(
                    type=ArtifactType.MARKDOWN,
                    filename="brief.md",
                    media_type="text/markdown",
                    content=b"stable",
                ),
            )
        )
        written = world.write_artifacts(result, Path("outputs"))
        assert written == (world.workspace / "outputs" / "brief.md",)
        assert written[0].read_bytes() == b"stable"
        assert world.write_artifacts(result, Path("outputs")) == written
        for unsafe in (Path("../escape"), (tmp_path / "absolute").resolve()):
            with pytest.raises(ValueError, match="relative|workspace"):
                world.write_artifacts(result, unsafe)
    finally:
        world.close()


@pytest.mark.parametrize(
    "directory",
    [Path("staging/../outputs"), Path(r"staging\..\outputs")],
)
def test_write_artifacts_rejects_lexical_parent_traversal_inside_workspace(
    tmp_path: Path,
    directory: Path,
) -> None:
    world = SimulationWorld.create(tmp_path / "world", START, 1)
    try:
        result = SkillRunResult.model_construct(artifacts=())
        with pytest.raises(ValueError, match="traversal"):
            world.write_artifacts(result, directory)
    finally:
        world.close()


def test_write_artifacts_rejects_unsafe_artifact_filename_before_writing(tmp_path: Path) -> None:
    world = SimulationWorld.create(tmp_path / "world", START, 1)
    try:
        unsafe = Artifact.model_construct(
            type=ArtifactType.MARKDOWN,
            filename="../escape.md",
            media_type="text/markdown",
            content=b"escape",
        )
        result = SkillRunResult.model_construct(artifacts=(unsafe,))
        with pytest.raises(ValueError, match="filename"):
            world.write_artifacts(result)
        assert not (tmp_path / "escape.md").exists()
    finally:
        world.close()
