"""End-to-end tests for the first built-in CSAF skill."""

from datetime import UTC, datetime, timedelta

from csaf.core import create_runtime
from csaf.schemas import MemoryKind, MemoryRecordCreate, SourceReference


def test_account_brief_is_grounded_cited_and_updates_memory() -> None:
    runtime = create_runtime()
    try:
        risk = runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.RISK,
                content="The migration may miss the renewal date.",
                logical_key="risk:migration",
                sources=(
                    SourceReference(source_type="meeting", source_id="meeting-17"),
                ),
                confidence=0.85,
            )
        )
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.PROFILE,
                content="Reduce time to onboard regional teams.",
                metadata={"topic": "business_goal"},
            )
        )
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="globex",
                kind=MemoryKind.RISK,
                content="This must not appear in Acme's brief.",
            )
        )

        result = runtime.runner.run("account-brief", {"customer_id": "acme"})

        assert result.output.risks[0].memory_record_id == risk.id
        assert result.output.risks[0].sources[0].source_id == "meeting-17"
        assert result.output.business_goals[0].text == "Reduce time to onboard regional teams."
        assert "globex" not in result.artifacts[0].content.decode().lower()
        assert f"`memory:{risk.id}`" in result.artifacts[0].content.decode()
        history = runtime.memory.history("acme", "account-brief:last-generated")
        assert history == list(result.memory_updates)
        assert history[0].metadata["record_count"] == 2
    finally:
        runtime.memory.close()


def test_account_brief_applies_optional_time_window() -> None:
    runtime = create_runtime()
    try:
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.TIMELINE,
                content="Old kickoff completed.",
                occurred_at=datetime.now(UTC) - timedelta(days=90),
            )
        )
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.TIMELINE,
                content="Recent workshop completed.",
                occurred_at=datetime.now(UTC) - timedelta(days=2),
            )
        )

        result = runtime.runner.run(
            "account-brief",
            {"customer_id": "acme", "time_window_days": 30},
        )

        assert [item.text for item in result.output.recent_activity] == [
            "Recent workshop completed."
        ]
        assert result.memory_updates[0].metadata["record_count"] == 1
    finally:
        runtime.memory.close()


def test_account_brief_handles_empty_memory_without_inventing_facts() -> None:
    runtime = create_runtime()
    try:
        result = runtime.runner.run("account-brief", {"customer_id": "new-customer"})

        assert result.output.executive_summary == (
            "No Customer Memory is available for new-customer."
        )
        assert result.output.risks == ()
        assert result.output.recommended_next_actions == (
            "Validate customer goals and capture the next agreed action.",
        )
    finally:
        runtime.memory.close()
