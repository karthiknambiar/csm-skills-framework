# Evaluation framework

CSAF evaluations are deterministic contract tests that run skills against
isolated Customer Memory fixtures. They complement unit tests by producing
versionable quality scores and a CI-readable regression report.

## Categories

| Category | Deterministic measurement |
| --- | --- |
| Accuracy | Exact values at declared dotted output paths |
| Completeness | Required non-empty output paths and artifact types |
| Hallucination | Absence of case-specific forbidden claims or entities |
| Citation quality | Count of memory-record citations and transcript excerpts |
| Consistency | Equivalent output across two independently seeded runs after volatile IDs and timestamps are removed |
| Memory updates | Minimum append counts by normalized memory kind |

Scores range from `0.0` to `1.0`. Categories default to a minimum score of `1.0`;
a golden case can set a lower explicit threshold when partial coverage is an
intentional baseline. Findings retain assertion-level details even when a partial
threshold passes.

## Golden case format

Golden datasets are JSON files containing one case or an array of cases:

```json
{
  "name": "account-brief-risk",
  "skill_name": "account-brief",
  "input": {"customer_id": "acme"},
  "memory": [
    {
      "customer_id": "acme",
      "kind": "risk",
      "content": "Migration is delayed."
    }
  ],
  "expected_values": {
    "risks.0.text": "Migration is delayed."
  },
  "required_output_paths": ["executive_summary", "risks.0.text"],
  "forbidden_terms": ["Globex"],
  "minimum_citations": 1,
  "expected_memory_writes": {"artifact": 1},
  "expected_artifacts": ["markdown"]
}
```

Inputs and memory fixtures must be self-contained. Cases execute twice in fresh
in-memory runtimes, preventing state leakage and making consistency meaningful.
Case names must be unique across a loaded dataset.

## Running regressions

Run the bundled golden dataset:

```bash
csaf evaluate evaluations/golden --report evaluation-report.json
```

The command prints a compact summary, writes detailed scores and findings when
`--report` is supplied, exits `0` when all thresholds pass, exits `1` for a
regression, and exits `2` for invalid datasets or execution errors.

CI runs this command after unit tests and uploads the JSON report for each Python
version. Add or update a golden case whenever an intentional public skill behavior
changes; do not lower thresholds merely to hide an unexplained regression.

## Limits and future graders

The initial framework measures structured, observable behavior and does not claim
to judge semantic writing quality. Model-based or human graders can later consume
the same cases and report contracts, but deterministic checks remain the required
baseline for provenance, memory effects, and regression safety.
