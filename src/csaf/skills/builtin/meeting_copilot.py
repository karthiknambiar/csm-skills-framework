"""Grounded Meeting Copilot skill with deterministic transcript extraction."""

import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from csaf.schemas import MemoryKind, MemoryRecordCreate, SourceReference
from csaf.skills import (
    Artifact,
    ArtifactType,
    Skill,
    SkillContext,
    SkillMetadata,
    SkillResultDraft,
)

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_SPEAKER_LINE = re.compile(r"^(?P<speaker>[^:\n]{1,80}):\s*(?P<text>.+)$")


class MeetingSentiment(StrEnum):
    """Coarse, explainable meeting sentiment."""

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    MIXED = "mixed"


class MeetingCopilotInput(BaseModel):
    """Transcript and meeting identity required for analysis and provenance."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=200)
    meeting_id: str = Field(min_length=1, max_length=500)
    transcript: str = Field(min_length=1, max_length=2_000_000)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attendees: tuple[str, ...] = ()

    @field_validator("customer_id", "meeting_id", "transcript", mode="before")
    @classmethod
    def reject_blank_strings(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class MeetingFinding(BaseModel):
    """A transcript-grounded extraction with its original excerpt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    excerpt: str
    speaker: str | None = None
    confidence: float = Field(default=0.75, ge=0.0, le=1.0)


class MeetingCopilotOutput(BaseModel):
    """Structured analysis suitable for CRM and follow-up workflows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: str
    meeting_id: str
    summary: tuple[MeetingFinding, ...]
    customer_goals: tuple[MeetingFinding, ...] = ()
    action_items: tuple[MeetingFinding, ...] = ()
    blockers: tuple[MeetingFinding, ...] = ()
    commitments: tuple[MeetingFinding, ...] = ()
    sentiment: MeetingSentiment
    risks: tuple[MeetingFinding, ...] = ()
    competitor_mentions: tuple[MeetingFinding, ...] = ()
    product_feedback: tuple[MeetingFinding, ...] = ()
    follow_up_email: str
    crm_notes: str


class _Utterance(BaseModel):
    speaker: str | None
    text: str


class MeetingCopilotSkill(Skill[MeetingCopilotInput, MeetingCopilotOutput]):
    """Extract actionable, source-linked meeting intelligence."""

    metadata = SkillMetadata(
        name="meeting-copilot",
        description="Analyze a transcript and append grounded meeting intelligence.",
        version="1.0.0",
        required_inputs=("customer_id", "meeting_id", "transcript"),
        optional_inputs=("occurred_at", "attendees"),
        memory_writes=(
            MemoryKind.MEETING,
            MemoryKind.TIMELINE,
            MemoryKind.COMMITMENT,
            MemoryKind.RISK,
            MemoryKind.FEATURE_REQUEST,
        ),
        artifacts=(ArtifactType.MARKDOWN,),
        evaluation_tests=(
            "transcript-grounding",
            "meeting-provenance",
            "memory-effects",
            "customer-isolation",
        ),
    )
    input_model = MeetingCopilotInput
    output_model = MeetingCopilotOutput

    def execute(
        self,
        skill_input: MeetingCopilotInput,
        context: SkillContext,
    ) -> SkillResultDraft[MeetingCopilotOutput]:
        utterances = self._utterances(skill_input.transcript)
        summary = tuple(self._finding(item) for item in utterances[:3])
        goals = self._matching(utterances, ("goal", "objective", "need to", "want to"))
        actions = self._matching(utterances, ("action:", "action item", "todo", "follow up"))
        blockers = self._matching(utterances, ("blocker", "blocked", "cannot", "can't"))
        commitments = self._matching(
            utterances,
            ("commit", "we will", "i will", "we'll", "i'll"),
        )
        risks = self._matching(
            utterances,
            ("risk", "concern", "delay", "escalat", "at risk"),
        )
        competitors = self._matching(
            utterances,
            ("competitor", "salesforce", "gainsight", "totango", "churnzero"),
        )
        feedback = self._matching(
            utterances,
            ("feedback", "feature request", "wish", "would like", "product request"),
        )
        sentiment = self._sentiment(utterances)
        output = MeetingCopilotOutput(
            customer_id=skill_input.customer_id,
            meeting_id=skill_input.meeting_id,
            summary=summary,
            customer_goals=goals,
            action_items=actions,
            blockers=blockers,
            commitments=commitments,
            sentiment=sentiment,
            risks=risks,
            competitor_mentions=competitors,
            product_feedback=feedback,
            follow_up_email=self._follow_up(skill_input.customer_id, summary, actions, commitments),
            crm_notes=self._crm_notes(summary, goals, actions, risks, sentiment),
        )
        return SkillResultDraft(
            output=output,
            memory_updates=self._memory_updates(skill_input, output),
            artifacts=(
                Artifact(
                    type=ArtifactType.MARKDOWN,
                    filename=f"{skill_input.meeting_id}-meeting-analysis.md",
                    media_type="text/markdown",
                    content=self._markdown(output).encode(),
                ),
            ),
        )

    @classmethod
    def _utterances(cls, transcript: str) -> tuple[_Utterance, ...]:
        utterances: list[_Utterance] = []
        for raw_line in transcript.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = _SPEAKER_LINE.match(line)
            speaker = match.group("speaker").strip() if match else None
            text = match.group("text").strip() if match else line
            for sentence in _SENTENCE_BOUNDARY.split(text):
                cleaned = sentence.strip(" -\t")
                if cleaned:
                    utterances.append(_Utterance(speaker=speaker, text=cleaned))
        return tuple(utterances)

    @staticmethod
    def _finding(utterance: _Utterance) -> MeetingFinding:
        return MeetingFinding(
            text=utterance.text,
            excerpt=utterance.text,
            speaker=utterance.speaker,
        )

    @classmethod
    def _matching(
        cls,
        utterances: tuple[_Utterance, ...],
        indicators: tuple[str, ...],
    ) -> tuple[MeetingFinding, ...]:
        return tuple(
            cls._finding(utterance)
            for utterance in utterances
            if any(indicator in utterance.text.casefold() for indicator in indicators)
        )

    @staticmethod
    def _sentiment(utterances: tuple[_Utterance, ...]) -> MeetingSentiment:
        text = " ".join(item.text for item in utterances).casefold()
        positive = any(word in text for word in ("great", "happy", "success", "excited", "love"))
        negative = any(
            word in text
            for word in ("unhappy", "frustrat", "disappoint", "concern", "blocked", "risk")
        )
        if positive and negative:
            return MeetingSentiment.MIXED
        if positive:
            return MeetingSentiment.POSITIVE
        if negative:
            return MeetingSentiment.NEGATIVE
        return MeetingSentiment.NEUTRAL

    @staticmethod
    def _follow_up(
        customer_id: str,
        summary: tuple[MeetingFinding, ...],
        actions: tuple[MeetingFinding, ...],
        commitments: tuple[MeetingFinding, ...],
    ) -> str:
        lines = [f"Subject: Follow-up from our meeting with {customer_id}", "", "Hello,", ""]
        if summary:
            lines.extend(("Thank you for the discussion. We covered:",))
            lines.extend(f"- {item.text}" for item in summary)
        agreed = actions + commitments
        if agreed:
            lines.extend(("", "Agreed next steps:"))
            lines.extend(f"- {item.text}" for item in agreed)
        lines.extend(("", "Please reply with any corrections or missing context.", "", "Best,"))
        return "\n".join(lines)

    @staticmethod
    def _crm_notes(
        summary: tuple[MeetingFinding, ...],
        goals: tuple[MeetingFinding, ...],
        actions: tuple[MeetingFinding, ...],
        risks: tuple[MeetingFinding, ...],
        sentiment: MeetingSentiment,
    ) -> str:
        def section(name: str, findings: tuple[MeetingFinding, ...]) -> list[str]:
            return [f"{name}:", *(f"- {item.text}" for item in findings or ())]

        lines = [f"Sentiment: {sentiment.value}"]
        for name, findings in (
            ("Summary", summary),
            ("Goals", goals),
            ("Actions", actions),
            ("Risks", risks),
        ):
            lines.extend(("", *section(name, findings)))
        return "\n".join(lines)

    @staticmethod
    def _memory_updates(
        skill_input: MeetingCopilotInput,
        output: MeetingCopilotOutput,
    ) -> tuple[MemoryRecordCreate, ...]:
        def source(finding: MeetingFinding) -> tuple[SourceReference, ...]:
            return (
                SourceReference(
                    source_type="transcript",
                    source_id=skill_input.meeting_id,
                    excerpt=finding.excerpt,
                    occurred_at=skill_input.occurred_at,
                ),
            )

        summary_text = " ".join(item.text for item in output.summary)
        records = [
            MemoryRecordCreate(
                customer_id=skill_input.customer_id,
                kind=MemoryKind.MEETING,
                logical_key=f"meeting:{skill_input.meeting_id}",
                content=summary_text or "Meeting transcript contained no analyzable statements.",
                metadata={
                    "meeting_id": skill_input.meeting_id,
                    "attendees": list(skill_input.attendees),
                    "sentiment": output.sentiment.value,
                },
                sources=tuple(source(item)[0] for item in output.summary),
                occurred_at=skill_input.occurred_at,
            ),
            MemoryRecordCreate(
                customer_id=skill_input.customer_id,
                kind=MemoryKind.TIMELINE,
                logical_key=f"timeline:meeting:{skill_input.meeting_id}",
                content=f"Meeting analyzed: {summary_text}",
                metadata={"meeting_id": skill_input.meeting_id},
                sources=tuple(source(item)[0] for item in output.summary),
                occurred_at=skill_input.occurred_at,
            ),
        ]
        for kind, prefix, findings in (
            (
                MemoryKind.COMMITMENT,
                "commitment",
                MeetingCopilotSkill._unique(output.commitments + output.action_items),
            ),
            (
                MemoryKind.RISK,
                "risk",
                MeetingCopilotSkill._unique(output.risks + output.blockers),
            ),
            (MemoryKind.FEATURE_REQUEST, "feature-request", output.product_feedback),
        ):
            records.extend(
                MemoryRecordCreate(
                    customer_id=skill_input.customer_id,
                    kind=kind,
                    logical_key=f"meeting:{skill_input.meeting_id}:{prefix}:{index}",
                    content=finding.text,
                    metadata={"meeting_id": skill_input.meeting_id, "speaker": finding.speaker},
                    sources=source(finding),
                    confidence=finding.confidence,
                    occurred_at=skill_input.occurred_at,
                )
                for index, finding in enumerate(findings, start=1)
            )
        return tuple(records)

    @staticmethod
    def _unique(findings: tuple[MeetingFinding, ...]) -> tuple[MeetingFinding, ...]:
        unique: list[MeetingFinding] = []
        seen: set[tuple[str | None, str]] = set()
        for finding in findings:
            key = (finding.speaker, finding.text.casefold())
            if key not in seen:
                seen.add(key)
                unique.append(finding)
        return tuple(unique)

    @staticmethod
    def _markdown(output: MeetingCopilotOutput) -> str:
        lines = [
            f"# Meeting Analysis: {output.meeting_id}",
            "",
            f"**Customer:** {output.customer_id}",
            f"**Sentiment:** {output.sentiment.value}",
        ]
        sections = (
            ("Summary", output.summary),
            ("Customer goals", output.customer_goals),
            ("Action items", output.action_items),
            ("Blockers", output.blockers),
            ("Commitments", output.commitments),
            ("Risks", output.risks),
            ("Competitor mentions", output.competitor_mentions),
            ("Product feedback", output.product_feedback),
        )
        for heading, findings in sections:
            lines.extend(("", f"## {heading}", ""))
            lines.extend(f"- {item.text} _(source: {item.excerpt})_" for item in findings)
            if not findings:
                lines.append("- None identified.")
        lines.extend(("", "## Follow-up email", "", output.follow_up_email))
        lines.extend(("", "## CRM notes", "", output.crm_notes))
        return "\n".join(lines) + "\n"
