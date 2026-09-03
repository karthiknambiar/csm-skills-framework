"""Deterministic, citation-first Account Brief vertical slice."""

from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from csaf.schemas import MemoryKind, MemoryRecord, MemoryRecordCreate, SourceReference
from csaf.skills import (
    Artifact,
    ArtifactType,
    Skill,
    SkillContext,
    SkillMetadata,
    SkillResultDraft,
)


class AccountBriefInput(BaseModel):
    """Inputs accepted by the Account Brief skill."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=200)
    time_window_days: int | None = Field(default=None, ge=1, le=3_650)


class Evidence(BaseModel):
    """A memory-backed statement with durable citations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    memory_record_id: UUID
    sources: tuple[SourceReference, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)


class AccountBriefOutput(BaseModel):
    """Structured account overview designed for API and artifact rendering."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: str
    generated_at: datetime
    executive_summary: str
    business_goals: tuple[Evidence, ...] = ()
    technical_environment: tuple[Evidence, ...] = ()
    stakeholders: tuple[Evidence, ...] = ()
    risks: tuple[Evidence, ...] = ()
    action_items: tuple[Evidence, ...] = ()
    opportunities: tuple[Evidence, ...] = ()
    product_feedback: tuple[Evidence, ...] = ()
    commitments: tuple[Evidence, ...] = ()
    adoption: tuple[Evidence, ...] = ()
    renewal_status: tuple[Evidence, ...] = ()
    recent_activity: tuple[Evidence, ...] = ()
    recommended_next_actions: tuple[str, ...] = ()


class AccountBriefSkill(Skill[AccountBriefInput, AccountBriefOutput]):
    """Build a grounded account overview from existing Customer Memory."""

    metadata = SkillMetadata(
        name="account-brief",
        description="Generate a citation-first overview of a customer account.",
        version="1.1.0",
        required_inputs=("customer_id",),
        optional_inputs=("time_window_days",),
        memory_reads=(
            MemoryKind.PROFILE,
            MemoryKind.STAKEHOLDER,
            MemoryKind.MEETING,
            MemoryKind.SUPPORT,
            MemoryKind.TIMELINE,
            MemoryKind.PRODUCT_USAGE,
            MemoryKind.ACTION_ITEM,
            MemoryKind.COMMITMENT,
            MemoryKind.RISK,
            MemoryKind.FEATURE_REQUEST,
            MemoryKind.RENEWAL,
            MemoryKind.HEALTH,
        ),
        memory_writes=(MemoryKind.ARTIFACT,),
        latest_memory_only=True,
        artifacts=(ArtifactType.MARKDOWN,),
        evaluation_tests=(
            "all-statements-cited",
            "customer-isolation",
            "time-window",
            "memory-update",
        ),
    )
    input_model = AccountBriefInput
    output_model = AccountBriefOutput

    def execute(
        self,
        skill_input: AccountBriefInput,
        context: SkillContext,
    ) -> SkillResultDraft[AccountBriefOutput]:
        generated_at = context.now
        records = self._in_time_window(
            context.supporting_memory,
            generated_at,
            skill_input.time_window_days,
        )
        groups = self._group(records)
        summary = self._executive_summary(skill_input.customer_id, groups)
        output = AccountBriefOutput(
            customer_id=skill_input.customer_id,
            generated_at=generated_at,
            executive_summary=summary,
            business_goals=self._topic(groups[MemoryKind.PROFILE], "business_goal"),
            technical_environment=self._topic(groups[MemoryKind.PROFILE], "technical_environment"),
            stakeholders=self._evidence(groups[MemoryKind.STAKEHOLDER]),
            risks=self._evidence(groups[MemoryKind.RISK]),
            action_items=self._evidence(groups[MemoryKind.ACTION_ITEM]),
            opportunities=(),
            product_feedback=self._evidence(groups[MemoryKind.FEATURE_REQUEST]),
            commitments=self._evidence(groups[MemoryKind.COMMITMENT]),
            adoption=self._evidence(groups[MemoryKind.PRODUCT_USAGE] + groups[MemoryKind.HEALTH]),
            renewal_status=self._evidence(groups[MemoryKind.RENEWAL]),
            recent_activity=self._evidence(self._recent_activity(groups)),
            recommended_next_actions=self._next_actions(groups),
        )
        markdown = self._render_markdown(output)
        return SkillResultDraft(
            output=output,
            memory_updates=(
                MemoryRecordCreate(
                    customer_id=skill_input.customer_id,
                    kind=MemoryKind.ARTIFACT,
                    logical_key="account-brief:last-generated",
                    content=f"Account Brief generated at {generated_at.isoformat()}.",
                    metadata={
                        "skill": self.metadata.name,
                        "skill_version": self.metadata.version,
                        "record_count": len(records),
                    },
                    confidence=1.0,
                    occurred_at=generated_at,
                ),
            ),
            artifacts=(
                Artifact(
                    type=ArtifactType.MARKDOWN,
                    filename=f"{skill_input.customer_id}-account-brief.md",
                    media_type="text/markdown",
                    content=markdown.encode(),
                ),
            ),
        )

    @staticmethod
    def _in_time_window(
        records: tuple[MemoryRecord, ...],
        generated_at: datetime,
        days: int | None,
    ) -> tuple[MemoryRecord, ...]:
        if days is None:
            return records
        since = generated_at - timedelta(days=days)
        return tuple(
            record for record in records if (record.occurred_at or record.created_at) >= since
        )

    @staticmethod
    def _group(records: tuple[MemoryRecord, ...]) -> dict[MemoryKind, list[MemoryRecord]]:
        groups = {kind: [] for kind in MemoryKind}
        for record in records:
            groups[record.kind].append(record)
        return groups

    @classmethod
    def _topic(cls, records: list[MemoryRecord], topic: str) -> tuple[Evidence, ...]:
        return cls._evidence(
            [record for record in records if record.metadata.get("topic") == topic]
        )

    @staticmethod
    def _evidence(records: list[MemoryRecord]) -> tuple[Evidence, ...]:
        return tuple(
            Evidence(
                text=record.content,
                memory_record_id=record.id,
                sources=record.sources,
                confidence=record.confidence,
            )
            for record in records
        )

    @staticmethod
    def _executive_summary(
        customer_id: str,
        groups: dict[MemoryKind, list[MemoryRecord]],
    ) -> str:
        total = sum(len(records) for records in groups.values())
        if total == 0:
            return f"No Customer Memory is available for {customer_id}."
        risks = len(groups[MemoryKind.RISK])
        commitments = len(groups[MemoryKind.COMMITMENT])
        stakeholders = len(groups[MemoryKind.STAKEHOLDER])
        return (
            f"{customer_id} has {total} relevant memory "
            f"{'record' if total == 1 else 'records'}, including "
            f"{risks} {'risk' if risks == 1 else 'risks'}, "
            f"{commitments} {'commitment' if commitments == 1 else 'commitments'}, "
            f"and {stakeholders} "
            f"{'stakeholder' if stakeholders == 1 else 'stakeholders'}."
        )

    @staticmethod
    def _recent_activity(
        groups: dict[MemoryKind, list[MemoryRecord]],
    ) -> list[MemoryRecord]:
        records = groups[MemoryKind.MEETING] + groups[MemoryKind.TIMELINE]
        selected = list(groups[MemoryKind.SUPPORT])
        by_meeting_id: dict[str, int] = {}
        for record in records:
            meeting_id = record.metadata.get("meeting_id")
            if not isinstance(meeting_id, str) or not meeting_id:
                selected.append(record)
                continue
            existing_index = by_meeting_id.get(meeting_id)
            if existing_index is None:
                by_meeting_id[meeting_id] = len(selected)
                selected.append(record)
            elif (
                record.kind is MemoryKind.TIMELINE
                and selected[existing_index].kind is not MemoryKind.TIMELINE
            ):
                selected[existing_index] = record
        return sorted(
            selected,
            key=lambda record: record.occurred_at or record.created_at,
            reverse=True,
        )[:10]

    @staticmethod
    def _without_prefix(text: str, *prefixes: str) -> str:
        for prefix in prefixes:
            if text.casefold().startswith(prefix.casefold()):
                return text[len(prefix) :].lstrip()
        return text

    @staticmethod
    def _next_actions(groups: dict[MemoryKind, list[MemoryRecord]]) -> tuple[str, ...]:
        actions = [
            "Review and assign the risk: "
            f"{AccountBriefSkill._without_prefix(record.content, 'risk:')}"
            for record in groups[MemoryKind.RISK]
        ]
        actions.extend(
            "Complete the action item: "
            f"{AccountBriefSkill._without_prefix(record.content, 'action item:', 'action:')}"
            for record in groups[MemoryKind.ACTION_ITEM]
        )
        if not actions:
            actions.append("Validate customer goals and capture the next agreed action.")
        return tuple(actions[:5])

    @staticmethod
    def _render_markdown(brief: AccountBriefOutput) -> str:
        lines = [
            f"# Account Brief: {brief.customer_id}",
            "",
            f"_Generated {brief.generated_at.isoformat()}_",
            "",
            "## Executive summary",
            "",
            brief.executive_summary,
        ]
        sections: tuple[tuple[str, tuple[Evidence, ...]], ...] = (
            ("Business goals", brief.business_goals),
            ("Technical environment", brief.technical_environment),
            ("Stakeholders", brief.stakeholders),
            ("Risks", brief.risks),
            ("Action Items", brief.action_items),
            ("Opportunities", brief.opportunities),
            ("Product Feedback", brief.product_feedback),
            ("Commitments", brief.commitments),
            ("Adoption", brief.adoption),
            ("Renewal status", brief.renewal_status),
            ("Recent activity", brief.recent_activity),
        )
        for heading, evidence in sections:
            lines.extend(("", f"## {heading}", ""))
            lines.extend(f"- {item.text} `memory:{item.memory_record_id}`" for item in evidence)
            if not evidence:
                lines.append("- No grounded information available.")
        lines.extend(("", "## Recommended next actions", ""))
        lines.extend(f"- {action}" for action in brief.recommended_next_actions)
        return "\n".join(lines) + "\n"
