"""Seed Customer Memory and print a cited Account Brief artifact."""

from csaf.core import create_runtime
from csaf.schemas import MemoryKind, MemoryRecordCreate, SourceReference


def main() -> None:
    runtime = create_runtime()
    try:
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.PROFILE,
                content="Reduce regional onboarding time by 25%.",
                metadata={"topic": "business_goal"},
                sources=(
                    SourceReference(source_type="crm", source_id="goal-1"),
                ),
            )
        )
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.RISK,
                content="The migration may miss the renewal date.",
                logical_key="risk:migration",
                sources=(
                    SourceReference(source_type="meeting", source_id="meeting-42"),
                ),
                confidence=0.8,
            )
        )
        result = runtime.runner.run("account-brief", {"customer_id": "acme"})
        print(result.artifacts[0].content.decode(), end="")
    finally:
        runtime.memory.close()


if __name__ == "__main__":
    main()
