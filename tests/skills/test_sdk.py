"""Contract tests and authoring example for the Skills SDK."""

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

import csaf.skills as skills
from csaf.memory import SQLiteMemoryStore
from csaf.schemas import MemoryKind, MemoryRecordCreate
from csaf.skills import (
    Artifact,
    ArtifactType,
    Skill,
    SkillContext,
    SkillMetadata,
    SkillRegistry,
    SkillResultDraft,
    SkillRunner,
)
from csaf.skills.errors import DuplicateSkillError, SkillContractError, SkillNotFoundError


def test_skills_exports_artifact_handler_contract() -> None:
    assert skills.ArtifactHandler == Callable[[tuple[Artifact, ...]], None]


class RiskDigestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1)
    heading: str = "Risk digest"


class RiskDigestOutput(BaseModel):
    count: int
    markdown: str


class RiskDigestSkill(Skill[RiskDigestInput, RiskDigestOutput]):
    metadata = SkillMetadata(
        name="risk-digest",
        description="Summarize current customer risks.",
        version="1.0.0",
        required_inputs=("customer_id",),
        optional_inputs=("heading",),
        memory_reads=(MemoryKind.RISK,),
        memory_writes=(MemoryKind.ARTIFACT,),
        artifacts=(ArtifactType.MARKDOWN,),
        evaluation_tests=("risk-count",),
    )
    input_model = RiskDigestInput
    output_model = RiskDigestOutput

    def execute(
        self,
        skill_input: RiskDigestInput,
        context: SkillContext,
    ) -> SkillResultDraft[RiskDigestOutput]:
        markdown = f"# {skill_input.heading}\n\nRisks: {len(context.supporting_memory)}"
        return SkillResultDraft(
            output=RiskDigestOutput(
                count=len(context.supporting_memory),
                markdown=markdown,
            ),
            memory_updates=(
                MemoryRecordCreate(
                    customer_id=skill_input.customer_id,
                    kind=MemoryKind.ARTIFACT,
                    logical_key=f"skill:{self.metadata.name}",
                    content="Generated the current risk digest.",
                ),
            ),
            artifacts=(
                Artifact(
                    type=ArtifactType.MARKDOWN,
                    filename="risk-digest.md",
                    media_type="text/markdown",
                    content=markdown.encode(),
                ),
            ),
        )


def test_runner_executes_full_declared_lifecycle() -> None:
    with SQLiteMemoryStore() as memory:
        memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.RISK,
                content="Renewal is at risk.",
                logical_key="risk:renewal",
            )
        )
        memory.append(
            MemoryRecordCreate(
                customer_id="globex",
                kind=MemoryKind.RISK,
                content="This other customer must remain isolated.",
            )
        )
        registry = SkillRegistry()
        registry.register(RiskDigestSkill())
        delivered: list[tuple[Artifact, ...]] = []

        artifact_handler = delivered.append

        result = SkillRunner(registry, memory).run(
            "risk-digest",
            {"customer_id": "acme"},
            artifact_handler=artifact_handler,
        )

        assert result.skill_name == "risk-digest"
        assert result.output == RiskDigestOutput(count=1, markdown="# Risk digest\n\nRisks: 1")
        assert result.model_dump()["output"]["count"] == 1
        assert result.memory_updates[0].revision == 1
        assert result.artifacts[0].filename == "risk-digest.md"
        assert delivered == [result.artifacts]
        assert len(memory.history("acme", "skill:risk-digest")) == 1


def test_runner_does_not_commit_memory_when_artifact_delivery_fails() -> None:
    with SQLiteMemoryStore() as memory:
        registry = SkillRegistry()
        registry.register(RiskDigestSkill())

        def fail_delivery(artifacts: tuple[Artifact, ...]) -> None:
            assert artifacts[0].filename == "risk-digest.md"
            raise OSError("artifact destination unavailable")

        try:
            SkillRunner(registry, memory).run(
                "risk-digest",
                {"customer_id": "acme"},
                artifact_handler=fail_delivery,
            )
        except OSError as error:
            assert str(error) == "artifact destination unavailable"
        else:
            raise AssertionError("artifact delivery failure should propagate")

        assert memory.history("acme", "skill:risk-digest") == []


def test_registry_rejects_duplicates_and_reports_unknown_skills() -> None:
    registry = SkillRegistry()
    registry.register(RiskDigestSkill())

    assert registry.names() == ("risk-digest",)
    assert len(registry) == 1

    try:
        registry.register(RiskDigestSkill())
    except DuplicateSkillError:
        pass
    else:
        raise AssertionError("duplicate registration should fail")

    try:
        registry.get("unknown")
    except SkillNotFoundError:
        pass
    else:
        raise AssertionError("unknown skill lookup should fail")


def test_registry_rejects_metadata_that_does_not_match_input_model() -> None:
    class InvalidDeclarationSkill(RiskDigestSkill):
        metadata = RiskDigestSkill.metadata.model_copy(
            update={"required_inputs": ("customer_id", "heading"), "optional_inputs": ()}
        )

    try:
        SkillRegistry().register(InvalidDeclarationSkill())
    except SkillContractError as error:
        assert "required_inputs" in str(error)
    else:
        raise AssertionError("drifting input declarations should fail")


def test_runner_validates_input_before_execution() -> None:
    with SQLiteMemoryStore() as memory:
        registry = SkillRegistry()
        registry.register(RiskDigestSkill())

        try:
            SkillRunner(registry, memory).run("risk-digest", {"unexpected": True})
        except SkillContractError as error:
            assert "invalid input" in str(error)
        else:
            raise AssertionError("invalid input should fail")


class CrossCustomerWriteSkill(RiskDigestSkill):
    def execute(
        self,
        skill_input: RiskDigestInput,
        context: SkillContext,
    ) -> SkillResultDraft[RiskDigestOutput]:
        return SkillResultDraft(
            output=RiskDigestOutput(count=0, markdown=""),
            memory_updates=(
                MemoryRecordCreate(
                    customer_id="another-customer",
                    kind=MemoryKind.ARTIFACT,
                    content="Invalid cross-customer effect.",
                ),
            ),
        )


def test_runner_rejects_cross_customer_memory_effects() -> None:
    with SQLiteMemoryStore() as memory:
        registry = SkillRegistry()
        registry.register(CrossCustomerWriteSkill())

        try:
            SkillRunner(registry, memory).run("risk-digest", {"customer_id": "acme"})
        except SkillContractError as error:
            assert "another customer" in str(error)
        else:
            raise AssertionError("cross-customer update should fail")
