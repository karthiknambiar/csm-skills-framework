# Skill development guide

A CSAF skill is a narrow, typed capability. It does not own transport handling,
memory retrieval, or persistence; `SkillRunner` supplies those lifecycle steps.

## 1. Define input and output

Use Pydantic models, reject unknown fields on inputs, and make `customer_id` a
required field. The SDK checks that metadata input declarations match the model.

```python
from pydantic import BaseModel, ConfigDict


class ExampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    tone: str = "concise"


class ExampleOutput(BaseModel):
    summary: str
```

## 2. Declare and implement the skill

Declare every memory category and artifact type the skill can produce. `execute`
returns proposed effects; it must not call `context.memory.append` itself.

```python
from csaf.schemas import MemoryKind, MemoryRecordCreate
from csaf.skills import Skill, SkillContext, SkillMetadata, SkillResultDraft


class ExampleSkill(Skill[ExampleInput, ExampleOutput]):
    input_model = ExampleInput
    output_model = ExampleOutput
    metadata = SkillMetadata(
        name="example-summary",
        description="Create a concise customer summary.",
        version="1.0.0",
        required_inputs=("customer_id",),
        optional_inputs=("tone",),
        memory_reads=(MemoryKind.PROFILE, MemoryKind.TIMELINE),
        memory_writes=(MemoryKind.ARTIFACT,),
        evaluation_tests=("summary-grounding",),
    )

    def execute(self, skill_input, context):
        summary = f"Retrieved {len(context.supporting_memory)} records."
        return SkillResultDraft(
            output=ExampleOutput(summary=summary),
            memory_updates=(
                MemoryRecordCreate(
                    customer_id=skill_input.customer_id,
                    kind=MemoryKind.ARTIFACT,
                    content="Generated an example summary.",
                ),
            ),
        )
```

## 3. Register and run

Registries are explicit dependencies rather than global state, which keeps tests
isolated and allows applications to choose which community skills they expose.

```python
from csaf.skills import SkillRegistry, SkillRunner

registry = SkillRegistry()
registry.register(ExampleSkill())
runner = SkillRunner(registry, memory_store)
result = runner.run("example-summary", {"customer_id": "acme"})
```

## Contract rules

- Skill names use lowercase kebab case and versions use `major.minor.patch`.
- Required and optional metadata inputs must exactly match the input model.
- Inputs must include a required, non-blank `customer_id`.
- Memory retrieval is limited to declared `memory_reads` categories.
- Proposed writes must target the input customer and a declared category.
- Produced artifact formats must be declared in metadata.
- Outputs are revalidated before effects are committed.
- Every skill should name its evaluation cases and ship lifecycle contract tests.

See `tests/skills/test_sdk.py` for an executable end-to-end authoring example.
