# Real-World Simulation Testing Design

**Status:** Approved design

**Date:** 2026-08-30

## Purpose

CSAF currently has strong unit, integration, packaging, security, and deterministic golden-case tests. Its evaluation suite, however, contains only three single-skill golden cases and five native-agent prompt scenarios. This design adds reproducible, multi-step simulations that represent real customer-success work across Meeting Copilot, Account Brief, QBR generation, Customer Memory, OfficeCLI, connectors, and native AI agents.

The suite must answer two different questions without conflating them:

1. Does CSAF preserve deterministic safety, provenance, state, and artifact contracts?
2. Do Codex, Claude, and Gemini use CSAF effectively in realistic conversations?

Deterministic failures block merges. Variable native-agent quality is measured through hard policy gates and an initially advisory rubric judge.

## Goals

- Model full customer lifecycles rather than isolated function calls.
- Reuse one scenario definition across deterministic and native-agent backends.
- Reproduce failures with fixed fixtures, clocks, seeds, models, and replay commands.
- Detect hallucination, cross-customer leakage, stale-state use, partial writes, unsafe installation behavior, broken recovery, invalid artifacts, and non-idempotent retries.
- Run a small Codex, Claude, and Gemini smoke matrix on pull requests and a full matrix nightly.
- Support synthetic fixtures plus manually sanitized, reviewed real examples without storing customer PII.
- Preserve the existing `csaf.evaluations` contracts while migrating incrementally.

## Non-Goals

- Replacing focused unit and integration tests.
- Making an LLM judge authoritative over deterministic safety checks.
- Running raw customer exports in CI.
- Dynamically downloading templates or external datasets during deterministic tests.
- Treating provider outages, rate limits, or judge disagreement as product success.
- Guaranteeing identical prose across native agents.

## Chosen Approach

Use a versioned scenario DSL. Each journey is data, not Python test code, and can be reviewed by engineers and customer-success practitioners. The same scenario runs through a deterministic CSAF backend and native-agent adapters.

Alternatives rejected:

- **Pytest-only journey classes:** easy to debug, but harder for non-engineers to review and prone to duplicated agent-specific cases.
- **Hosted evaluation platform:** useful dashboards, but adds privacy, cost, network, and vendor dependencies while weakening offline reproducibility.

## Architecture

Journey definitions live under `evaluations/simulations/`. Each definition contains:

- Schema version, stable scenario ID, tags, severity, and fixed seed.
- Synthetic customer identities and initial Customer Memory.
- Ordered events such as meetings, support incidents, product-usage changes, stakeholder changes, renewals, and connector responses.
- Skill or agent invocations with explicit inputs.
- Optional fault injections.
- Expected state transitions, artifacts, citations, policies, and prohibited behavior.
- Deterministic grading thresholds and optional quality rubric dimensions.

One engine executes a journey through two backend classes:

- **Deterministic backend:** calls CSAF runtime APIs directly in an isolated world. It runs without network access and blocks merges on any failed hard assertion.
- **Native-agent backend:** presents the same journey to Codex, Claude, or Gemini, records tool calls and user-visible behavior, and grades policy plus outcome contracts. It runs a smoke subset on pull requests and the full matrix nightly.

Run flow:

1. Validate scenario and fixture provenance.
2. Create isolated world with fixed clock, temporary workspace, Customer Memory database, fake connectors, and recording Office renderer.
3. Apply initial state.
4. Execute each event and invocation in order.
5. Capture sanitized state and artifact snapshots after every step.
6. Run deterministic hard graders.
7. For native runs, apply policy graders and the quality rubric.
8. Emit JSON, Markdown, and JUnit reports with an exact replay command.

## Components

### ScenarioLoader

Validates DSL schema versions, unique IDs, allowed event types, fixture references, expected outcomes, fixed seeds, and incompatible settings. Unknown fields fail validation. Scenario files load in deterministic order.

### SimulationWorld

Owns one isolated run: temporary workspace, SQLite Customer Memory, fixed clock, customer identities, fake connectors, recording Office renderer, and artifact directory. No state is shared between scenarios or agent runs.

### JourneyRunner

Executes ordered steps, enforces timeouts, captures pre-step and post-step snapshots, and stops or continues according to explicit scenario failure policy. It records each command, result, memory change, artifact, and exception in structured form.

### FailureInjector

Provides named deterministic faults instead of ad hoc monkeypatches in scenario files. Initial faults:

- Missing OfficeCLI.
- Connector timeout and rate limit.
- Corrupt or tampered QBR template.
- Renderer failure before artifact commit.
- Partial write attempt.
- Duplicate meeting delivery.
- Stale memory followed by newer contradictory evidence.
- Malformed or truncated transcript.

Faults must define activation point, duration, expected recovery, and cleanup behavior.

### DeterministicGrader

Hard assertions cover:

- Exact customer and tenant isolation.
- Citation and source provenance for every factual statement.
- Required and forbidden values.
- Memory kinds, logical keys, revisions, and idempotency.
- No partial memory or artifact changes after failed operations.
- Valid artifact types, names, media types, and Office requests.
- Template selection and bundled-template integrity.
- Repeatability after removing volatile values.
- Recovery after an injected transient failure.

All deterministic hard assertions must pass.

### NativeAgentAdapter

Defines one interface for Codex, Claude, and Gemini. Each adapter supplies a fixed model configuration, creates an isolated agent workspace, sends scenario turns, captures commands and tool calls, enforces budgets, collects artifacts, sanitizes output, and returns a common transcript record.

Adapters must classify provider failures separately from product failures. One infrastructure retry is allowed for pull-request smoke runs. Product failures are never retried into a pass.

### PolicyGrader

Native-agent hard gates verify:

- Correct CSAF workflow selection.
- Exact deterministic commands where required.
- Explicit consent before installation or persistent changes.
- Clear disclosure that OfficeCLI 1.0.143 is mandatory when installation is proposed.
- No silent download, dynamic QBR-template search, or unsupported success claim.
- All detected agent targets are presented accurately.
- Missing facts, owners, dates, and customer data are not invented.
- Recovery instructions are complete and scoped.

### RubricJudge

Grades variable outputs for usefulness, prioritization, clarity, executive readiness, and actionability. It receives the scenario, sanitized output, deterministic findings, and fixed rubric. It cannot override a hard failure.

Judge results remain advisory until calibrated against 50 human-labeled outputs containing good, borderline, and failed examples. Promotion to a merge gate requires a documented agreement threshold and bias check across source agent and model.

### SimulationReporter

Emits:

- Machine-readable JSON report.
- Human-readable Markdown summary.
- JUnit results for CI annotations.
- Per-agent comparison for native runs.
- Sanitized transcript and state-diff artifacts.
- Exact local replay command containing scenario ID, backend, agent, model, and seed.

## Scenario Catalog

Initial suite contains 18 journeys.

### Customer Lifecycle

1. New customer with sparse memory.
2. Healthy adoption growth.
3. Declining usage before renewal.
4. Executive sponsor departure.
5. Security review blocking rollout.
6. Escalated support incident.
7. Expansion opportunity with incomplete evidence.
8. Multi-quarter QBR progression.

### Meeting Realism

9. Noisy transcript with filler, interruptions, and repeated statements.
10. Conflicting commitments from two speakers.
11. Missing owner and due date without invention.
12. Duplicate meeting ingestion followed by retry.
13. Competitor mention that must not automatically become churn risk.

### Data Integrity and Operations

14. Acme/Globex tenant-isolation attack.
15. Fresh evidence contradicting stale memory.
16. Connector timeout followed by successful retry.
17. Corrupt user QBR template with safe failure and recovery guidance.
18. OfficeCLI absent, consent denied, then approved recovery.

Each journey spans multiple state transitions where relevant. Example: a meeting updates Customer Memory, Account Brief reflects new grounded state, and QBR uses the same citations and template policy. Repeating the journey verifies idempotency and revision behavior.

## Fixture and Privacy Policy

Synthetic fixtures are the default and require no production secrets. Sanitized real examples may be added only when:

- A reviewer confirms all names, companies, email addresses, domains, IDs, URLs, free-text identifiers, and commercial values are fictionalized or removed.
- Secret and PII scans pass.
- A provenance record identifies the sanitizer, review date, source class, permitted use, and fixture hash without retaining original customer identity.
- The fixture is committed as a minimal case, not a raw export.

Deterministic CI never accesses external networks. Native runs send only approved synthetic or sanitized fixtures.

## CI Policy

### Pull Requests

- Run all 18 journeys through the deterministic backend.
- Run three native smoke journeys for each of Codex, Claude, and Gemini:
  - Consent-first OfficeCLI installation and recovery.
  - Meeting-to-memory-to-account-brief flow.
  - QBR generation with user or bundled template policy.
- Require 100% deterministic and native policy-gate success.
- Permit one retry only for classified infrastructure failures.
- Keep rubric-judge results advisory.
- Enforce per-run token, cost, duration, and concurrency budgets.

### Nightly

- Run the full 18-scenario matrix against Codex, Claude, and Gemini.
- Pin agent, model, prompt, adapter, scenario, and rubric versions.
- Compare results with the rolling baseline and latest release.
- Alert on hard-gate failures, quality-score drops, latency regressions, unsupported claims, or increased infrastructure failure rates.
- Preserve sanitized artifacts according to a fixed retention policy.

## Error Handling

- Schema and fixture errors fail before any scenario executes.
- Deterministic failures include exact step and state diff.
- Agent timeouts stop the run and preserve sanitized partial evidence.
- Rate limits, provider outages, and unavailable models receive an infrastructure classification.
- Judge failure leaves hard results intact and marks quality score unavailable.
- Redaction failure prevents artifact upload.
- Cleanup failure is reported separately and cannot turn a product failure into a pass.
- Replay commands never contain secrets or raw credentials.

## File Boundaries

- `src/csaf/simulations/schema.py`: scenario and step contracts.
- `src/csaf/simulations/loader.py`: DSL and fixture loading.
- `src/csaf/simulations/world.py`: isolated state and clock.
- `src/csaf/simulations/runner.py`: ordered execution and snapshots.
- `src/csaf/simulations/faults.py`: named fault injection.
- `src/csaf/simulations/graders.py`: deterministic and policy grading.
- `src/csaf/simulations/reporting.py`: JSON, Markdown, JUnit, replay output.
- `src/csaf/simulations/adapters/`: deterministic, Codex, Claude, Gemini, and judge adapters.
- `evaluations/simulations/`: journey definitions and synthetic fixtures.
- `evaluations/calibration/`: sanitized human labels and provenance.
- `tests/simulations/`: schema, runner, safety, failure, adapter, grading, and replay tests.
- `.github/workflows/`: pull-request smoke and nightly matrix workflows.
- `docs/testing/`: scenario authoring, privacy review, triage, and replay guides.

Existing `src/csaf/evaluations/` remains supported. Golden cases migrate into the new runner incrementally, and existing CLI/API behavior remains unchanged during the first implementation phase.

## Delivery Phases

### Phase 1: Deterministic Framework

Build scenario schema, loader, isolated world, runner, snapshots, fault injection, graders, and reports. Convert current golden cases into multi-step journeys, add all 18 scenarios, and make this suite merge-blocking.

### Phase 2: Native-Agent Harness

Build shared adapter contract and Codex, Claude, and Gemini adapters. Add capture, redaction, budgets, retries, replay, hard policy grading, and the three-scenario pull-request smoke matrix.

### Phase 3: Quality Evaluation

Add fixed rubric and judge adapter, create 50 human-labeled calibration outputs, enable full nightly matrix, and add trend reports. Promote judge scores to blocking only after documented calibration acceptance.

## Acceptance Criteria

- All 18 journeys execute deterministically without network access.
- Every journey is replayable by stable scenario ID and seed.
- Deterministic suite detects planted provenance, isolation, idempotency, rollback, artifact, template, and recovery regressions.
- Codex, Claude, and Gemini implement the shared adapter contract.
- Pull requests run all deterministic journeys plus three native smoke journeys per agent.
- Nightly workflow runs all 54 native agent/scenario combinations within configured budgets.
- Provider failures are distinguished from product failures.
- Rubric results remain advisory until the 50-output calibration gate passes.
- Reports contain no secrets or PII and include exact sanitized replay commands.
- Existing evaluation and product tests remain green throughout incremental migration.
