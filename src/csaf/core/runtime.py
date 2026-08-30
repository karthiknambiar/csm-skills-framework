"""Application composition root shared by transport adapters."""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from csaf.core.clock import IdFactory, Now, utc_now
from csaf.memory import MemoryStore, SQLiteMemoryStore
from csaf.office import OfficeArtifactRenderer, OfficeCLIArtifactRenderer
from csaf.skills import SkillRegistry, SkillRunner
from csaf.skills.builtin import AccountBriefSkill, MeetingCopilotSkill, QBRSkill


@dataclass(frozen=True, slots=True)
class Runtime:
    """Explicit dependencies used by the CLI, API, and future applications."""

    memory: MemoryStore
    skills: SkillRegistry
    runner: SkillRunner


def create_runtime(
    database: str | Path = ":memory:",
    office_renderer: OfficeArtifactRenderer | None = None,
    *,
    now: Now = utc_now,
    id_factory: IdFactory = uuid4,
) -> Runtime:
    """Build the default local runtime without relying on global mutable state."""

    memory = SQLiteMemoryStore(database, now=now, id_factory=id_factory)
    skills = SkillRegistry()
    skills.register(AccountBriefSkill())
    skills.register(MeetingCopilotSkill())
    skills.register(QBRSkill(office_renderer or OfficeCLIArtifactRenderer()))
    return Runtime(
        memory=memory,
        skills=skills,
        runner=SkillRunner(skills, memory, now=now, id_factory=id_factory),
    )
