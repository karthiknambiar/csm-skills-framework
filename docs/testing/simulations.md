# Deterministic customer journey simulations

Simulations exercise Account Brief, Meeting Copilot, QBR, and local fixture ingestion across a sequence of customer events. Each scenario gets a temporary SQLite database, workspace, deterministic IDs, and a clock starting at `2026-01-01T00:00:00Z`. The global `--database` option is ignored for this command. No live customer system, network credential, hosted model, or OfficeCLI download is needed.

The Office renderer records requests and returns deterministic placeholder bytes. These tests check runtime contracts, not Office document layout or whether an assistant follows installation instructions. The `officecli-consent-recovery` journey is an availability proxy: `office_missing` represents the unavailable dependency and `clear_faults` permits recovery. It never requests or grants real consent. Real installer consent belongs in the later native-agent harness; the existing native offline smoke workflow remains separate.

## Run and replay

From a development checkout with `.[dev]` installed, run:

```bash
csaf --database :memory: simulate evaluations/simulations --report-dir simulation-results
csaf simulate evaluations/simulations --scenario connector-timeout-retry --seed 305 --report-dir simulation-results
csaf simulate evaluations/simulations/connector-timeout-retry.json --fixture-root evaluations/simulations/fixtures --report-dir simulation-results
python -m pytest tests/simulations tests/test_documentation.py
```

The dataset argument accepts one JSON file or a directory of top-level `*.json` files, loaded in sorted order without descending into `fixtures`. Each file contains one scenario, and IDs must be unique across the dataset. Repeat `--scenario` to select distinct IDs; unknown or duplicate selections are rejected. `--seed` overrides the stored seed only when exactly one scenario is selected. Fixtures default to the dataset directory's `fixtures` subdirectory, or the JSON file's sibling `fixtures` directory. Use `--fixture-root` to select another local fixture directory.

Exit code 0 means every selected journey and expectation passed. Exit code 1 means an execution or grading failure; the reports retain the evidence. Exit code 2 means invalid dataset or selection, unsafe replay configuration, or an infrastructure failure such as being unable to write reports. An infrastructure failure may leave no usable report, so inspect stderr first.

The report directory contains `simulation-report.json`, `simulation-report.md`, and `simulation-junit.xml`. JSON carries scenario identity, seed, step evidence, findings, and `replay_argv`. Re-run that argument array with a subprocess API using `shell=False`; do not join it into shell code. Keep the same checkout, dataset, fixtures, and seed when comparing results. Reports remove sensitive diagnostic details, but sanitization is not permission to feed them private data. Inspect reports before sharing them.

For a failed journey, check the step's `error_type`, `error_message`, and before/after snapshots. A failed `execution` finding means the runner could not complete the journey. Other finding codes identify the failed expectation, such as an output mismatch, customer boundary violation, or artifact contract failure. Fix an incorrect fixture or expectation only after checking the underlying customer behavior; do not weaken a contract merely to make CI pass.

CI runs the command after unit tests in both Python 3.11 and 3.12 matrix jobs on every pull request. It uploads each matrix job's `simulation-results` directory even after failure, with unique artifact names and a 14-day retention period. Golden evaluations and native offline smoke tests remain in place. The simulation steps add no credentials or downloads.

## Scenario schema

The current schema is `schema_version: 1` (an integer, not a string or boolean). Required fields are `id`, `title`, integer `seed`, nonempty unique `customers`, nonempty `steps`, and nonempty `expectations`. IDs use lowercase letters, digits, and hyphen-separated words, starting with a letter. Unknown fields, duplicate JSON keys, non-finite numbers, and undeclared customer references are rejected. The source of truth is [schema.py](../../src/csaf/simulations/schema.py).

Every step has a `type` and may have an explicit unique `id`. Otherwise the runner assigns `step-1`, `step-2`, and so on. Expectations that specify `step_id` must reference an explicit ID, so give meaningful IDs to steps you intend to check.

`seed_memory` takes `records`, an array of domain `MemoryRecordCreate` inputs. Include `customer_id`, `kind`, and `content`; use `logical_key`, `occurred_at`, metadata, and `sources` when the journey depends on revision history or provenance. `run_skill` takes the registered `skill` name and its JSON `input`; the bundled names are `account-brief`, `meeting-copilot`, and `qbr`. Skill inputs remain subject to their own runtime validation.

`advance_time` takes integer `seconds` from 1 through 31,536,000. It moves the simulation clock, not the host clock. `set_fault` takes `fault` and optional `remaining_calls` from 1 through 100 (default 1). Supported faults are `office_missing`, `office_render_failure`, `artifact_commit_failure`, `connector_timeout`, `connector_rate_limit`, and `corrupt_template`. `clear_faults` clears active faults. Faults are consumed by the relevant operation; `corrupt_template` applies when a render request has a template path.

`ingest_fixture` takes `customer_id` and `fixture`, a local JSON basename beneath the fixture root. Absolute paths, traversal, and symlinks are rejected. Its JSON object contains only a `records` array. Each record needs a unique nonblank `id`, `kind`, and `content`; optional fields are `logical_key`, `metadata`, `occurred_at`, and `confidence`. The connector supplies source provenance as `simulation_fixture` with a `fixture:<basename>#<record-number>` URI. Text transcript fixtures are reference material; this step does not ingest text files.

Both `run_skill` and `ingest_fixture` accept `expect_error`. Match an exception class name or one of the supported stable messages: `QBR artifact rendering failed`, `simulated connector timeout`, or `simulated connector rate limit`. A matching error counts as a successful expected-error step and lets the journey continue. The runner restores appended memory, artifact files, Office requests, and the deterministic ID counter to the checkpoint. Consumed fault calls are not restored. If the expected error never occurs, the runner rolls back the effects and stops with `ExpectedErrorNotRaised`. An unexpected error stops the journey and preserves its failure evidence.

## Expectations and evidence

`output_equals` takes `path` and JSON `value`; `output_present` takes `path` and rejects missing, null, empty string, and empty collections. Paths use dots for object keys and numeric list indices, for example `citations.0.source_id`. Without `step_id`, these grade the last successful skill output; with it, they grade that step's output. `forbidden_term` takes `term` and checks a case-insensitive substring across all successful serialized skill outputs, or the selected step's output.

`memory_count` takes `customer_id` and nonnegative `count`, counting all persisted records including historical revisions in final memory. `memory_revision` takes `customer_id`, `logical_key`, and positive `revision`, checking the highest stored revision for that key. Both always inspect final memory, even if `step_id` is supplied. Built-in skills use the latest revision for each logical key when building current customer context; older revisions remain available in the simulation snapshots. Preserve source excerpts and timestamps so revised conclusions remain traceable to their evidence.

`artifact_types` takes a nonempty ordered `values` array, such as `["powerpoint", "word"]`. It checks type order, filenames, media types, and content integrity rather than mere file existence. It covers all emitted artifacts unless targeted to a step. `citation_minimum` takes positive `count` and checks citation evidence in the last or selected output. `no_cross_customer_data` checks customer boundaries across outputs, memory effects, and artifacts, optionally scoped to a step; it also reconciles reported effects against snapshots. It is not a general-purpose PII detector.

`no_partial_effects` requires `step_id` and checks that a failed or expected-error step leaves memory, artifacts, and Office requests unchanged. Include it alongside `expect_error` when testing retries. This proves the simulation's rollback boundary; it does not prove the behavior of an external installer or service.

## Add a journey

Start with one JSON file in `evaluations/simulations`, named after its unique scenario ID. Keep one scenario per JSON file and describe the customer event in the title. This complete example runs without external fixtures:

```json
{
  "schema_version": 1,
  "id": "new-account-review",
  "title": "Review an account before evidence arrives",
  "seed": 17,
  "customers": ["acme"],
  "steps": [
    {
      "id": "brief",
      "type": "run_skill",
      "skill": "account-brief",
      "input": {"customer_id": "acme"}
    }
  ],
  "expectations": [
    {"type": "output_equals", "step_id": "brief", "path": "customer_id", "value": "acme"},
    {"type": "memory_count", "customer_id": "acme", "count": 1},
    {"type": "no_cross_customer_data"}
  ]
}
```

Account Brief writes a summary record even when the account starts empty, which is why this example expects one memory record. Build out the event sequence with explicit timestamps, logical keys, and source evidence. Assert the behavior that distinguishes the journey: a corrected revision, a missing-evidence result, a recovered retry, or tenant isolation. Run the single scenario first, then the full suite. The bundled dataset tests maintain an explicit scenario and fixture catalog; update those catalogs deliberately when adding a new case.

Use synthetic data for the public CI corpus. Sanitized customer-derived data needs explicit approval and a documented PII review before it can be considered; the current corpus contract accepts only `source_class: "synthetic"`, so sanitized data is not currently accepted by that gate. Never commit raw customer exports, credentials, personal contact details, or private transcripts. Review scenario text and embedded metadata as well as separate fixture files.

Every fixture file is tracked in [provenance.json](../../evaluations/simulations/fixtures/provenance.json) with `path`, `sha256`, `source_class`, `pii_reviewed: true`, `reviewed_on`, and `permitted_use: "public-ci"`. Review the exact bytes for PII and secrets, compute their SHA-256 digest, and record the review date. Preserve repository-stable line endings so hashes are portable. After an edit, repeat the review and update the hash; a checksum proves byte identity, not privacy. The current corpus tests pin the synthetic catalog and review date, so any policy or date change must be reviewed with the corresponding tests. Run the repository secret scan as an additional check, not as a replacement for review.
