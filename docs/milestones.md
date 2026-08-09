# Delivery milestones

CSAF will be delivered incrementally. Each milestone should be independently
reviewable, tested, and documented; a milestone should not pre-implement later
layers merely to demonstrate breadth.

## 1. Project scaffolding and architecture — complete

- Establish an installable `src`-layout Python package.
- Define package boundaries and dependency direction.
- Add baseline lint, test, and CI configuration.
- Document development and contribution workflows.

## 2. Memory engine and schemas — complete

- Define normalized memory records, source references, revisions, and queries.
- Implement append-only SQLite storage and version history.
- Introduce deterministic structured/text search behind a replaceable store protocol.
- Test customer isolation, provenance, confidence, and temporal behavior.

## 3. Skills SDK — complete

- Define typed skill metadata, inputs, outputs, artifacts, and memory effects.
- Implement registry and execution lifecycle.
- Provide a skill authoring template and contract tests.

## 4. CLI and REST API — complete

- Expose the same application services through Typer and FastAPI.
- Add configuration, error mapping, OpenAPI, and transport-level tests.

## 5. Account Brief vertical slice — complete

- Build the first end-to-end skill across memory, model, artifact, CLI, and API.
- Produce a cited structured response and Markdown artifact.
- Record the generation event in Customer Memory.

## 6. Meeting Copilot — complete

- Analyze transcripts into summaries, actions, risks, commitments, and feedback.
- Generate follow-up email and CRM-note outputs.
- Append meeting-derived records with transcript provenance.

## 7. QBR generation with OfficeCLI — complete

- Generate PowerPoint and Word QBR artifacts from memory.
- Support template-based creation and updates to existing QBRs.
- Version every generated artifact.

## 8. Connector framework — complete

- Define discovery, authentication, pagination, checkpoint, and normalization
  contracts.
- Provide local file, JSON, CSV, and Markdown reference connectors.
- Add fixtures and conformance tests for community connectors.

## 9. Evaluation framework — complete

- Define golden datasets and deterministic evaluation runners.
- Measure accuracy, completeness, citations, consistency, hallucination, and
  memory effects.
- Add regression reporting suitable for CI.

## 10. Documentation and examples — complete

- Publish skill, connector, workflow, artifact, prompt, and memory guides.
- Add tutorials and runnable end-to-end examples.
- Document community contribution and compatibility policies.
