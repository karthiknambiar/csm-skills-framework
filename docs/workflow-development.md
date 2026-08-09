# Workflow development guide

Workflows compose registered skills into a business process while preserving each
skill's validation and memory lifecycle. CSAF currently exposes the primitives for
composition through `SkillRegistry` and `SkillRunner`; a persistent workflow
orchestrator is intentionally outside the completed foundation milestones.

## Composition pattern

```python
brief = runtime.runner.run("account-brief", {"customer_id": "acme"})
meeting = runtime.runner.run(
    "meeting-copilot",
    {
        "customer_id": "acme",
        "meeting_id": "meeting-42",
        "transcript": transcript,
    },
)
```

The second skill sees committed memory from the first only when that memory kind
is declared in its `memory_reads`. Do not call a skill's `execute` method directly;
that bypasses input/output validation and effect enforcement.

## Rules for workflow authors

1. Pass structured outputs between steps instead of parsing rendered artifacts.
2. Let `SkillRunner` commit effects before starting a dependent step.
3. Keep one customer identifier throughout a customer-scoped workflow.
4. Make retry boundaries explicit; skills append revisions, so blind retries can
   create additional history.
5. Persist workflow state outside skill internals when durable orchestration is
   introduced.
6. Treat artifacts as outputs, not as the source of truth; structured output and
   Customer Memory remain authoritative.
7. Test both the successful path and failure after every step boundary.

## Planned workflow metadata

A future orchestration contract should declare a stable name/version, ordered or
conditional steps, input/output schema, retry policy, customer scope, and failure
compensation. It should invoke registered public skill names so community skills
remain replaceable. This guide does not claim that durable workflow execution,
queues, or distributed retries are implemented today.
