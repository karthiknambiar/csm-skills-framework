"""Quarterly Business Review generation backed by Customer Memory and OfficeCLI."""

from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from csaf.office import (
    OfficeArtifactRenderer,
    OfficeCLIArtifactRenderer,
    OfficeCLIDoctor,
    OfficeCLIError,
    OfficeFormat,
    OfficeOperation,
    OfficeRenderRequest,
    OfficeSection,
)
from csaf.schemas import MemoryKind, MemoryRecord, MemoryRecordCreate, SourceReference
from csaf.skills import (
    Artifact,
    ArtifactType,
    Skill,
    SkillContext,
    SkillMetadata,
    SkillResultDraft,
)
from csaf.skills.errors import SkillExecutionError
from csaf.templates.qbr import default_qbr_powerpoint, default_qbr_word


class QBRInput(BaseModel):
    """Inputs for creating or updating a customer's quarterly review."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=200)
    quarter: str = Field(pattern=r"^\d{4}-Q[1-4]$")
    powerpoint_template: Path | None = None
    word_template: Path | None = None
    existing_powerpoint: Path | None = None
    existing_word: Path | None = None


class QBREvidence(BaseModel):
    """A QBR statement grounded in an immutable memory record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    memory_record_id: UUID
    sources: tuple[SourceReference, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)


class QBROutput(BaseModel):
    """Structured review content shared by PowerPoint and Word renderers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: str
    quarter: str
    generated_at: datetime
    artifact_version: int = Field(ge=1)
    executive_summary: str
    adoption_trends: tuple[QBREvidence, ...] = ()
    support_metrics: tuple[QBREvidence, ...] = ()
    business_outcomes: tuple[QBREvidence, ...] = ()
    roadmap: tuple[QBREvidence, ...] = ()
    recommendations: tuple[QBREvidence, ...] = ()
    goals: tuple[QBREvidence, ...] = ()
    next_quarter_plan: tuple[QBREvidence, ...] = ()
    powerpoint_operation: OfficeOperation
    word_operation: OfficeOperation


class QBRSkill(Skill[QBRInput, QBROutput]):
    """Build a cited QBR and render PowerPoint and Word artifacts."""

    metadata = SkillMetadata(
        name="qbr",
        description="Generate or update a cited quarterly business review.",
        version="1.1.0",
        required_inputs=("customer_id", "quarter"),
        optional_inputs=(
            "powerpoint_template",
            "word_template",
            "existing_powerpoint",
            "existing_word",
        ),
        memory_reads=(
            MemoryKind.PROFILE,
            MemoryKind.SUPPORT,
            MemoryKind.TIMELINE,
            MemoryKind.PRODUCT_USAGE,
            MemoryKind.ROADMAP,
            MemoryKind.ACTION_ITEM,
            MemoryKind.COMMITMENT,
            MemoryKind.RISK,
            MemoryKind.FEATURE_REQUEST,
            MemoryKind.QBR,
            MemoryKind.SUCCESS_PLAN,
            MemoryKind.RENEWAL,
            MemoryKind.HEALTH,
        ),
        memory_writes=(MemoryKind.QBR, MemoryKind.ARTIFACT),
        latest_memory_only=False,
        artifacts=(ArtifactType.POWERPOINT, ArtifactType.WORD),
        evaluation_tests=(
            "qbr-citation-coverage",
            "office-artifact-generation",
            "existing-artifact-update",
            "artifact-versioning",
        ),
    )
    input_model = QBRInput
    output_model = QBROutput

    def __init__(self, renderer: OfficeArtifactRenderer) -> None:
        self._renderer = renderer

    def preflight(self) -> None:
        """Check the configured local renderer before Customer Memory is read."""

        if isinstance(self._renderer, OfficeCLIArtifactRenderer):
            OfficeCLIDoctor(self._renderer).preflight()

    def execute(
        self,
        skill_input: QBRInput,
        context: SkillContext,
    ) -> SkillResultDraft[QBROutput]:
        groups = self._group(context.supporting_memory)
        version = 1 + sum(
            1
            for record in groups[MemoryKind.QBR]
            if record.metadata.get("quarter") == skill_input.quarter
        )
        risks = self._evidence(groups[MemoryKind.RISK] + groups[MemoryKind.RENEWAL])
        actions = self._evidence(groups[MemoryKind.ACTION_ITEM])
        commitments = self._evidence(groups[MemoryKind.COMMITMENT])
        output = QBROutput(
            customer_id=skill_input.customer_id,
            quarter=skill_input.quarter,
            generated_at=datetime.now(UTC),
            artifact_version=version,
            executive_summary=self._summary(skill_input.customer_id, skill_input.quarter, groups),
            adoption_trends=self._evidence(
                groups[MemoryKind.PRODUCT_USAGE] + groups[MemoryKind.HEALTH]
            ),
            support_metrics=self._evidence(groups[MemoryKind.SUPPORT]),
            business_outcomes=self._topic(groups[MemoryKind.PROFILE], "business_outcome"),
            roadmap=self._evidence(groups[MemoryKind.ROADMAP]),
            recommendations=risks,
            goals=self._topic(groups[MemoryKind.PROFILE], "business_goal"),
            next_quarter_plan=actions
            + commitments
            + self._evidence(groups[MemoryKind.SUCCESS_PLAN]),
            powerpoint_operation=self._operation(skill_input.existing_powerpoint),
            word_operation=self._operation(skill_input.existing_word),
        )
        sections = self._sections(output)
        with ExitStack() as template_stack:
            if skill_input.existing_powerpoint is not None:
                powerpoint_template = None
                powerpoint_source = "existing"
            elif skill_input.powerpoint_template is not None:
                powerpoint_template = skill_input.powerpoint_template
                powerpoint_source = "user"
            else:
                powerpoint_template = template_stack.enter_context(default_qbr_powerpoint())
                powerpoint_source = "bundled"

            if skill_input.existing_word is not None:
                word_template = None
                word_source = "existing"
            elif skill_input.word_template is not None:
                word_template = skill_input.word_template
                word_source = "user"
            else:
                word_template = template_stack.enter_context(default_qbr_word())
                word_source = "bundled"

            try:
                powerpoint = self._renderer.render(
                    OfficeRenderRequest(
                        format=OfficeFormat.POWERPOINT,
                        operation=output.powerpoint_operation,
                        title=f"{skill_input.customer_id} {skill_input.quarter} QBR",
                        subtitle=f"Version {version}",
                        sections=sections,
                        template_path=powerpoint_template,
                        existing_path=skill_input.existing_powerpoint,
                    )
                )
                word = self._renderer.render(
                    OfficeRenderRequest(
                        format=OfficeFormat.WORD,
                        operation=output.word_operation,
                        title=f"{skill_input.customer_id} {skill_input.quarter} QBR",
                        subtitle=f"Version {version}",
                        sections=sections,
                        template_path=word_template,
                        existing_path=skill_input.existing_word,
                    )
                )
            except OfficeCLIError as error:
                raise SkillExecutionError(f"QBR artifact rendering failed: {error}") from error
        basename = f"{skill_input.customer_id}-{skill_input.quarter}-qbr-v{version}"
        return SkillResultDraft(
            output=output,
            memory_updates=self._memory_updates(
                skill_input,
                output,
                basename,
                powerpoint_source=powerpoint_source,
                word_source=word_source,
            ),
            artifacts=(
                Artifact(
                    type=ArtifactType.POWERPOINT,
                    filename=f"{basename}.pptx",
                    media_type=(
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                    ),
                    content=powerpoint,
                ),
                Artifact(
                    type=ArtifactType.WORD,
                    filename=f"{basename}.docx",
                    media_type=(
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ),
                    content=word,
                ),
            ),
        )

    @staticmethod
    def _operation(existing: Path | None) -> OfficeOperation:
        return OfficeOperation.UPDATE if existing is not None else OfficeOperation.CREATE

    @staticmethod
    def _group(records: tuple[MemoryRecord, ...]) -> dict[MemoryKind, list[MemoryRecord]]:
        groups = {kind: [] for kind in MemoryKind}
        for record in records:
            groups[record.kind].append(record)
        return groups

    @staticmethod
    def _evidence(records: list[MemoryRecord]) -> tuple[QBREvidence, ...]:
        return tuple(
            QBREvidence(
                text=record.content,
                memory_record_id=record.id,
                sources=record.sources,
                confidence=record.confidence,
            )
            for record in records
        )

    @classmethod
    def _topic(cls, records: list[MemoryRecord], topic: str) -> tuple[QBREvidence, ...]:
        return cls._evidence(
            [record for record in records if record.metadata.get("topic") == topic]
        )

    @staticmethod
    def _summary(
        customer_id: str,
        quarter: str,
        groups: dict[MemoryKind, list[MemoryRecord]],
    ) -> str:
        total = sum(len(items) for kind, items in groups.items() if kind is not MemoryKind.QBR)
        if total == 0:
            return f"No grounded customer information is available for {customer_id} {quarter}."
        return (
            f"{customer_id} {quarter} review is grounded in {total} current memory records, "
            f"with {len(groups[MemoryKind.RISK])} risks and "
            f"{len(groups[MemoryKind.COMMITMENT])} active commitments."
        )

    @staticmethod
    def _sections(output: QBROutput) -> tuple[OfficeSection, ...]:
        evidence_sections = (
            ("Adoption trends", output.adoption_trends),
            ("Support metrics", output.support_metrics),
            ("Business outcomes", output.business_outcomes),
            ("Roadmap", output.roadmap),
            ("Recommendations", output.recommendations),
            ("Goals", output.goals),
            ("Next-quarter plan", output.next_quarter_plan),
        )
        sections = [OfficeSection(title="Executive summary", bullets=(output.executive_summary,))]
        sections.extend(
            OfficeSection(
                title=title,
                bullets=tuple(item.text for item in evidence),
                citations=tuple(f"memory:{item.memory_record_id}" for item in evidence),
            )
            for title, evidence in evidence_sections
        )
        return tuple(sections)

    @staticmethod
    def _memory_updates(
        skill_input: QBRInput,
        output: QBROutput,
        basename: str,
        *,
        powerpoint_source: str,
        word_source: str,
    ) -> tuple[MemoryRecordCreate, ...]:
        generated_at = output.generated_at
        common_metadata = {
            "quarter": skill_input.quarter,
            "artifact_version": output.artifact_version,
            "generated_at": generated_at.isoformat(),
        }
        return (
            MemoryRecordCreate(
                customer_id=skill_input.customer_id,
                kind=MemoryKind.QBR,
                logical_key=f"qbr:{skill_input.quarter}",
                content=output.executive_summary,
                metadata={
                    **common_metadata,
                    "template_source": {
                        "powerpoint": powerpoint_source,
                        "word": word_source,
                    },
                },
                occurred_at=generated_at,
            ),
            MemoryRecordCreate(
                customer_id=skill_input.customer_id,
                kind=MemoryKind.ARTIFACT,
                logical_key=f"artifact:qbr:{skill_input.quarter}:powerpoint",
                content=f"Generated {basename}.pptx",
                metadata={
                    **common_metadata,
                    "format": "powerpoint",
                    "template_source": powerpoint_source,
                },
                occurred_at=generated_at,
            ),
            MemoryRecordCreate(
                customer_id=skill_input.customer_id,
                kind=MemoryKind.ARTIFACT,
                logical_key=f"artifact:qbr:{skill_input.quarter}:word",
                content=f"Generated {basename}.docx",
                metadata={
                    **common_metadata,
                    "format": "word",
                    "template_source": word_source,
                },
                occurred_at=generated_at,
            ),
        )
