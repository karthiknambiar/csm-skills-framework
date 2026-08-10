"""End-to-end QBR tests with an injected Office renderer."""

from pathlib import Path

from csaf.core import Runtime, create_runtime
from csaf.office import OfficeFormat, OfficeOperation, OfficeRenderRequest
from csaf.schemas import MemoryKind, MemoryRecordCreate


class RecordingRenderer:
    def __init__(self) -> None:
        self.requests: list[OfficeRenderRequest] = []

    def render(self, request: OfficeRenderRequest) -> bytes:
        self.requests.append(request)
        return f"{request.format.value}:{request.operation.value}:{request.title}".encode()


def seed_qbr_memory(runtime: Runtime) -> None:
    memory = runtime.memory
    for record in (
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.PRODUCT_USAGE,
            content="Weekly active users increased by 18%.",
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.SUPPORT,
            content="Median resolution time fell to 8 hours.",
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.PROFILE,
            content="Reduce onboarding time by 25%.",
            metadata={"topic": "business_outcome"},
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.ROADMAP,
            content="SSO rollout is planned for October.",
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.RISK,
            content="The data migration remains delayed.",
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.ACTION_ITEM,
            content="Schedule the security review.",
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.COMMITMENT,
            content="Complete migration validation next month.",
        ),
    ):
        memory.append(record)


def test_qbr_generates_cited_powerpoint_and_word_and_versions_memory() -> None:
    renderer = RecordingRenderer()
    runtime = create_runtime(office_renderer=renderer)
    try:
        seed_qbr_memory(runtime)

        result = runtime.runner.run("qbr", {"customer_id": "acme", "quarter": "2026-Q3"})

        assert result.output.artifact_version == 1
        assert result.output.adoption_trends[0].text.endswith("18%.")
        assert result.output.support_metrics[0].text.endswith("8 hours.")
        assert result.output.business_outcomes[0].text.startswith("Reduce onboarding")
        assert result.output.roadmap[0].memory_record_id
        assert result.output.recommendations[0].text.startswith("The data migration")
        assert [item.text for item in result.output.next_quarter_plan] == [
            "Schedule the security review.",
            "Complete migration validation next month.",
        ]
        assert runtime.skills.get("qbr").metadata.version == "1.1.0"
        assert [artifact.type.value for artifact in result.artifacts] == [
            "powerpoint",
            "word",
        ]
        serialized = result.model_dump(mode="json")
        assert isinstance(serialized["artifacts"][0]["content"], str)
        assert renderer.requests[0].sections[1].citations[0].startswith("memory:")
        assert runtime.memory.history("acme", "qbr:2026-Q3")[0].revision == 1
        assert len(result.memory_updates) == 3
    finally:
        runtime.memory.close()


def test_qbr_updates_existing_artifacts_and_increments_versions(tmp_path: Path) -> None:
    renderer = RecordingRenderer()
    runtime = create_runtime(office_renderer=renderer)
    try:
        first = runtime.runner.run("qbr", {"customer_id": "acme", "quarter": "2026-Q3"})
        existing_powerpoint = tmp_path / first.artifacts[0].filename
        existing_word = tmp_path / first.artifacts[1].filename
        existing_powerpoint.write_bytes(first.artifacts[0].content)
        existing_word.write_bytes(first.artifacts[1].content)

        second = runtime.runner.run(
            "qbr",
            {
                "customer_id": "acme",
                "quarter": "2026-Q3",
                "existing_powerpoint": existing_powerpoint,
                "existing_word": existing_word,
            },
        )

        assert second.output.artifact_version == 2
        assert second.output.powerpoint_operation is OfficeOperation.UPDATE
        assert second.output.word_operation is OfficeOperation.UPDATE
        assert renderer.requests[-2].format is OfficeFormat.POWERPOINT
        assert renderer.requests[-2].existing_path == existing_powerpoint
        assert renderer.requests[-1].format is OfficeFormat.WORD
        assert renderer.requests[-1].existing_path == existing_word
        assert [record.revision for record in runtime.memory.history("acme", "qbr:2026-Q3")] == [
            1,
            2,
        ]
        assert second.artifacts[0].filename.endswith("qbr-v2.pptx")
    finally:
        runtime.memory.close()
