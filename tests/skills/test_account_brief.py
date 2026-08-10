"""End-to-end tests for the first built-in CSAF skill."""

from datetime import UTC, datetime, timedelta

from csaf.core import create_runtime
from csaf.schemas import MemoryKind, MemoryRecordCreate, SourceReference
from csaf.skills.builtin.account_brief import AccountBriefSkill


def test_account_brief_is_grounded_cited_and_updates_memory() -> None:
    runtime = create_runtime()
    try:
        risk = runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.RISK,
                content="The migration may miss the renewal date.",
                logical_key="risk:migration",
                sources=(SourceReference(source_type="meeting", source_id="meeting-17"),),
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


def test_account_brief_separates_actions_feedback_and_opportunities() -> None:
    runtime = create_runtime()
    try:
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.ACTION_ITEM,
                content="Follow up with security.",
            )
        )
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.FEATURE_REQUEST,
                content="Bulk provisioning requested.",
            )
        )

        result = runtime.runner.run("account-brief", {"customer_id": "acme"})

        assert result.output.action_items[0].text == "Follow up with security."
        assert result.output.product_feedback[0].text == "Bulk provisioning requested."
        assert result.output.opportunities == ()
        markdown = result.artifacts[0].content.decode()
        assert "## Action Items\n" in markdown
        assert "## Product Feedback\n" in markdown
    finally:
        runtime.memory.close()


def test_account_brief_deduplicates_only_meeting_timeline_pairs_before_limit() -> None:
    runtime = create_runtime()
    try:
        base_time = datetime.now(UTC) - timedelta(hours=30)
        for kind, content, hours, metadata in (
            (
                MemoryKind.MEETING,
                "Raw meeting summary.",
                18,
                {"meeting_id": "meeting-42"},
            ),
            (
                MemoryKind.TIMELINE,
                "Meeting analyzed: concise summary.",
                20,
                {"meeting_id": "meeting-42"},
            ),
            (
                MemoryKind.SUPPORT,
                "Support case discussed during the meeting.",
                19,
                {"meeting_id": "meeting-42"},
            ),
        ):
            runtime.memory.append(
                MemoryRecordCreate(
                    customer_id="acme",
                    kind=kind,
                    content=content,
                    metadata=metadata,
                    occurred_at=base_time + timedelta(hours=hours),
                )
            )
        for hours in range(9):
            runtime.memory.append(
                MemoryRecordCreate(
                    customer_id="acme",
                    kind=MemoryKind.SUPPORT,
                    content=f"Independent support event {hours}.",
                    occurred_at=base_time + timedelta(hours=hours),
                )
            )

        result = runtime.runner.run("account-brief", {"customer_id": "acme"})

        assert [item.text for item in result.output.recent_activity] == [
            "Meeting analyzed: concise summary.",
            "Support case discussed during the meeting.",
            *(f"Independent support event {hours}." for hours in range(8, 0, -1)),
        ]
    finally:
        runtime.memory.close()


def test_account_brief_uses_singular_grammar_and_clean_recommendations() -> None:
    runtime = create_runtime()
    try:
        for kind, content in (
            (MemoryKind.RISK, "Risk: Renewal approval is delayed."),
            (MemoryKind.ACTION_ITEM, "Action item: Follow up with security."),
            (MemoryKind.COMMITMENT, "Commitment: Send the security response."),
            (MemoryKind.STAKEHOLDER, "Priya owns the renewal."),
        ):
            runtime.memory.append(
                MemoryRecordCreate(
                    customer_id="acme",
                    kind=kind,
                    content=content,
                )
            )

        result = runtime.runner.run("account-brief", {"customer_id": "acme"})

        assert "1 risk, 1 commitment, and 1 stakeholder" in (result.output.executive_summary)
        assert result.output.recommended_next_actions == (
            "Review and assign the risk: Renewal approval is delayed.",
            "Complete the action item: Follow up with security.",
        )
        assert AccountBriefSkill.metadata.version == "1.1.0"
    finally:
        runtime.memory.close()


def test_account_brief_uses_singular_memory_record_grammar() -> None:
    runtime = create_runtime()
    try:
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.RISK,
                content="Renewal approval is delayed.",
            )
        )

        result = runtime.runner.run("account-brief", {"customer_id": "acme"})

        assert "has 1 relevant memory record, including 1 risk" in (result.output.executive_summary)
    finally:
        runtime.memory.close()


def test_account_brief_recommends_an_explicit_action_without_generic_fallback() -> None:
    runtime = create_runtime()
    try:
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.ACTION_ITEM,
                content="Action: Schedule the architecture review.",
            )
        )

        result = runtime.runner.run("account-brief", {"customer_id": "acme"})

        assert result.output.recommended_next_actions == (
            "Complete the action item: Schedule the architecture review.",
        )
    finally:
        runtime.memory.close()
