"""End-to-end tests for the built-in Meeting Copilot skill."""

from datetime import UTC, datetime

from csaf.core import create_runtime
from csaf.schemas import MemoryKind, MemoryQuery

TRANSCRIPT = """\
Alex: Our goal is to launch the regional team by October. We are excited about adoption.
Priya: Risk: the data migration is delayed and the team is blocked on API access.
Alex: We will send the mapping document on Friday.
Priya: Action: follow up with security about API access.
Alex: Product feedback: we would like bulk user provisioning.
Priya: We are also comparing the rollout with Gainsight.
"""


def test_meeting_copilot_extracts_grounded_structured_outputs() -> None:
    runtime = create_runtime()
    try:
        result = runtime.runner.run(
            "meeting-copilot",
            {
                "customer_id": "acme",
                "meeting_id": "meeting-42",
                "transcript": TRANSCRIPT,
                "occurred_at": "2026-08-08T15:00:00Z",
                "attendees": ["Alex", "Priya"],
            },
        )

        assert result.output.customer_goals[0].speaker == "Alex"
        assert result.output.risks[0].excerpt.startswith("Risk:")
        assert result.output.blockers[0].speaker == "Priya"
        assert result.output.commitments[0].text == (
            "We will send the mapping document on Friday."
        )
        assert result.output.action_items[0].text.startswith("Action:")
        assert result.output.product_feedback[0].speaker == "Alex"
        assert result.output.competitor_mentions[0].text.endswith("Gainsight.")
        assert result.output.sentiment.value == "mixed"
        assert "Agreed next steps" in result.output.follow_up_email
        assert "Sentiment: mixed" in result.output.crm_notes
        assert b"# Meeting Analysis: meeting-42" in result.artifacts[0].content
    finally:
        runtime.memory.close()


def test_meeting_copilot_appends_provenance_to_memory() -> None:
    runtime = create_runtime()
    try:
        occurred_at = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)
        result = runtime.runner.run(
            "meeting-copilot",
            {
                "customer_id": "acme",
                "meeting_id": "meeting-42",
                "transcript": TRANSCRIPT,
                "occurred_at": occurred_at,
            },
        )

        meeting = runtime.memory.history("acme", "meeting:meeting-42")[0]
        assert meeting.kind is MemoryKind.MEETING
        assert meeting.metadata["sentiment"] == "mixed"
        assert meeting.sources[0].source_id == "meeting-42"
        assert meeting.sources[0].occurred_at == occurred_at
        assert runtime.memory.history("acme", "timeline:meeting:meeting-42")
        assert any(update.kind is MemoryKind.COMMITMENT for update in result.memory_updates)
        assert any(update.kind is MemoryKind.RISK for update in result.memory_updates)
        assert any(update.kind is MemoryKind.FEATURE_REQUEST for update in result.memory_updates)
        risk_updates = [
            update for update in result.memory_updates if update.kind is MemoryKind.RISK
        ]
        assert len({update.content for update in risk_updates}) == len(risk_updates)
    finally:
        runtime.memory.close()


def test_meeting_copilot_keeps_customer_memory_isolated() -> None:
    runtime = create_runtime()
    try:
        runtime.runner.run(
            "meeting-copilot",
            {
                "customer_id": "acme",
                "meeting_id": "meeting-42",
                "transcript": TRANSCRIPT,
            },
        )

        assert runtime.memory.search(MemoryQuery(customer_id="globex")) == []
        assert runtime.memory.search(MemoryQuery(customer_id="acme"))
    finally:
        runtime.memory.close()


def test_meeting_copilot_handles_minimal_transcript_without_invention() -> None:
    runtime = create_runtime()
    try:
        result = runtime.runner.run(
            "meeting-copilot",
            {
                "customer_id": "acme",
                "meeting_id": "meeting-minimal",
                "transcript": "Customer: Thanks for meeting today.",
            },
        )

        assert result.output.summary[0].text == "Thanks for meeting today."
        assert result.output.risks == ()
        assert result.output.commitments == ()
        assert result.output.product_feedback == ()
        assert result.output.sentiment.value == "neutral"
    finally:
        runtime.memory.close()
