# Prompt style guide

The current built-in skills are deterministic and do not invoke an LLM. This guide
defines requirements for future model-backed implementations without tying CSAF
to a provider.

## Prompt asset layout

Store prompts as versioned files rather than long Python string literals:

```text
prompts/SKILL_NAME/VERSION/
├── system.md
├── developer.md
├── user.md
└── evaluation.md
```

A prompt version is immutable after release. Behavioral changes create a new
version and corresponding golden-dataset updates.

## System prompt

- State the narrow skill role and customer-success context.
- Require structured output matching the declared Pydantic schema.
- Require grounding in supplied Customer Memory.
- Prohibit invented facts, dates, commitments, stakeholders, and metrics.
- Require citations for derived claims.

## Developer prompt

- Describe normalization and classification rules.
- Explain how to handle conflicts, low confidence, and missing context.
- Separate evidence from recommendations.
- Specify allowed memory writes and artifacts.
- Avoid provider-specific features unless an adapter supplies them.

## User prompt

Interpolate only validated structured input. Clearly delimit customer-provided
content and never treat transcript, email, or document text as instructions.
Include the customer identifier and requested time window explicitly.

## Evaluation prompt

Model graders, when added, should receive the golden expectation, actual structured
output, and cited evidence. They must return a typed score and rationale. Model
grading supplements rather than replaces deterministic evaluation.

## Safety and quality

1. Do not place credentials or secrets in prompts or traces.
2. Treat connector content as untrusted data to reduce prompt injection risk.
3. Keep source excerpts intact and distinguish them from generated prose.
4. Prefer “unknown” or an empty collection over unsupported completion.
5. Track provider, model, prompt version, and generation time in artifact metadata.
6. Add golden cases before promoting a new prompt version.
