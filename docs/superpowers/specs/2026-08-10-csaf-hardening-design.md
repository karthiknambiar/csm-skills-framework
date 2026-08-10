# CSAF Plug-and-Play Hardening Design

**Date:** 2026-08-10
**Status:** Approved for implementation planning
**Target:** Pre-1.0 hardening release

## Purpose

Make CSAF's built-in skills dependable for non-technical users while preserving
the framework's memory-first, deterministic, vendor-neutral architecture. This
release fixes the operational and content-quality defects found during the
repository usability review, connects QBR generation to a real fully local
OfficeCLI implementation, and prevents credentials from entering the repository.

The target OfficeCLI is
[`iOfficeAI/OfficeCLI`](https://github.com/iOfficeAI/OfficeCLI), the Apache-2.0,
single-binary, local document engine. CSAF will not integrate the unrelated
hosted AI document generator that uses the same product name.

## Goals

1. Make Account Brief and Meeting Copilot reliable from Python, CLI, and REST.
2. Ensure artifact-delivery failures do not commit Customer Memory effects.
3. Make generic skill invocation practical on Windows without shell-specific
   JSON quoting knowledge.
4. Improve deterministic classification, deduplication, and prose quality
   without an LLM, hosted service, or API credential.
5. Make QBR generation use the documented local OfficeCLI command surface and
   provide actionable installation diagnostics.
6. Close packaging, evaluation, metadata, compatibility-warning, licensing, and
   secret-hygiene gaps.

## Non-goals

- CSAF will not add built-in API authentication. Authentication, authorization,
  request limits, audit logging, and tenant access policy remain responsibilities
  of the embedding application.
- CSAF will not install OfficeCLI automatically.
- CSAF will not call a hosted model or accept model-provider API keys.
- CSAF will not replace SQLite or introduce a general transaction coordinator.
- CSAF will not add workflow orchestration beyond the existing skill runner.
- CSAF will not promise pixel-identical rendering across Office applications.

## Architecture

The existing dependency direction remains unchanged. The hardening release adds
focused behavior at four existing boundaries:

1. `SkillRunner` gains an optional artifact-delivery callback. It validates the
   skill draft, invokes the callback, and only then appends memory effects.
2. The CLI uses one reusable atomic artifact writer and exposes file-based JSON
   input for generic skill execution.
3. The Office adapter targets the local OfficeCLI `create`, `batch`, `validate`,
   and `view ... issues` commands and exposes structured diagnostics.
4. Built-in deterministic skills use a clearer memory taxonomy and rendering
   rules, without adding a model-provider dependency.

No domain package will import from `cli` or `api`. File-system delivery remains a
transport concern supplied to the runner through the callback. OfficeCLI remains
behind `OfficeArtifactRenderer` and is injected through `create_runtime`.

## Artifact delivery and memory ordering

`SkillRunner.run` will accept an optional keyword-only artifact handler with the
logical contract:

```python
ArtifactHandler = Callable[[tuple[Artifact, ...]], None]
```

Execution order becomes:

1. Validate input.
2. Retrieve declared memory.
3. Execute the skill.
4. Validate output and declared effects.
5. Deliver artifacts through the optional handler.
6. Append memory updates.
7. Return `SkillRunResult`.

If artifact delivery raises, step 6 is not reached. The CLI maps file-system
errors to a concise `Error:` message and exit code `2`; it does not emit a
traceback. This directly fixes the observed behavior where a failed `--output`
write still created Account Brief revisions or meeting-derived records.

The CLI artifact writer will:

- create missing parent directories;
- reject artifact filenames containing directory traversal or path separators;
- write every artifact to a temporary file in its destination directory;
- finish all temporary writes before replacing destinations;
- clean temporary files on failure; and
- use `os.replace` for same-filesystem atomic replacement.

The callback guarantees that a failed delivery does not commit memory. It does
not attempt distributed atomicity between the filesystem and SQLite; a later
SQLite failure can leave a delivered file, which is outside this focused fix.

## CLI usability

Generic skill execution will accept exactly one of:

- `--input JSON`, retained for compatibility; or
- `--input-file PATH`, recommended for PowerShell and non-technical workflows.

Providing neither or both returns exit code `2` with a direct explanation. The
documentation will show Windows PowerShell examples using `--input-file` and
will explain `--%` only as an advanced inline-JSON alternative.

`skill run` will omit binary/base64 artifact content from terminal JSON by
default. `--include-artifact-content` restores the current full transport shape.
An optional `--output-dir` writes returned artifacts through the same atomic
handler. Dedicated Account Brief, Meeting, and QBR commands continue to write
human-facing files by default when their output options are supplied.

## Deterministic skill quality

### Memory taxonomy

Add `MemoryKind.ACTION_ITEM = "action_item"`. Meeting Copilot will write action
items to this category and commitments only to `COMMITMENT`. This is a compatible
enum addition, but it changes built-in skill memory effects and therefore
requires minor skill-version bumps.

### Meeting Copilot

Meeting Copilot will:

- classify explicit tasks and follow-ups as action items;
- keep promises such as â€œwe willâ€ as commitments;
- deduplicate blockers that are also risks while retaining one cited risk record;
- normalize display prefixes such as `Risk:` and `Action:` without changing the
  source excerpt used for provenance;
- retain the meeting record as the canonical meeting summary; and
- retain one timeline event whose metadata points to the meeting record.

Its public output already separates `action_items`, `commitments`, and
`product_feedback`, so no output fields are removed. Its metadata changes from
version `1.0.0` to `1.1.0` because memory writes change.

### Account Brief

Account Brief will add backward-compatible defaulted output fields:

```text
action_items: tuple[Evidence, ...]
product_feedback: tuple[Evidence, ...]
```

`opportunities` remains present for compatibility but no longer treats every
feature request as an opportunity. Feature requests appear under Product
feedback. Explicit action items appear separately and drive recommended next
actions. Commitments remain promises, not tasks.

Recent activity will deduplicate meeting/timeline pairs by `meeting_id`, prefer
the concise timeline event, and then apply the existing recency limit. Summary
counts will use singular/plural forms correctly. Generated recommendations will
strip category prefixes before adding their own labels, preventing text such as
`Review and assign the risk: Risk: ...`.

The Account Brief skill metadata changes to version `1.1.0` because its output
schema and declared memory reads change.

### QBR

QBR will read action items as part of the next-quarter plan while keeping
commitments distinct. Its skill metadata changes to version `1.1.0` because its
declared reads change and its renderer behavior is corrected.

## Local OfficeCLI integration

### Supported runtime

The adapter targets `iOfficeAI/OfficeCLI` version `1.0.137` or newer. That floor
provides atomic batch behavior and the local create/validate/issue-inspection
surface required by CSAF.

The default command flow for a new artifact is:

```text
officecli create <working-file>
officecli batch <working-file> --input <batch-json> --json
officecli validate <working-file> --json
officecli view <working-file> issues --json
```

The adapter will force immediate persistence with
`OFFICECLI_RESIDENT_FLUSH=each` for mutation commands and will not depend on an
open resident. Every subprocess uses an argument array, a private temporary
directory, captured output, a timeout, and no shell interpolation.

For a template or existing file, the adapter copies the source into the private
working path and applies an atomic batch to the copy. The original is never
modified. PowerPoint creation uses a title slide plus one slide per section.
Word creation uses a title, subtitle, section headings, bullet paragraphs, and
citation paragraphs. Styling is deterministic and encoded in the generated
batch commands.

Validation failure is fatal. Reported document issues are returned in diagnostic
details; structural issues classified by OfficeCLI as errors are fatal, while
non-fatal formatting observations do not prevent delivery.

### Diagnostics

Add `csaf office doctor` with human-readable output by default and `--json` for
automation. It performs:

1. PATH lookup for `officecli`.
2. `officecli --version` parsing and minimum-version validation.
3. A temporary PPTX creation, batch write, validation, and issue inspection.
4. A temporary DOCX creation, batch write, validation, and issue inspection.
5. Cleanup of all temporary files.

The result reports each check as `pass`, `fail`, or `skip`, includes redacted
diagnostics, and exits `0` only when QBR rendering is ready. Missing installations
show the official Windows PowerShell and macOS/Linux installation commands from
the selected local OfficeCLI project, but CSAF never runs those commands.

QBR generation performs a quick preflight (binary and version) before executing
the skill. The render itself supplies the full create/batch/validate guarantee.

## Evaluations and tests

All behavior changes follow test-first development. Coverage will include:

- artifact-handler failure prevents every declared memory append;
- Account Brief and Meeting output failures return code `2` without tracebacks;
- missing output directories are created;
- multiple artifacts are staged before destination replacement;
- unsafe artifact filenames are rejected;
- `--input-file` works on Windows-compatible paths;
- mutually exclusive generic input options fail clearly;
- base64 artifact bodies are omitted by default and opt-in when requested;
- action items and commitments remain distinct in output and memory;
- feature requests render as product feedback;
- recent meeting activity is deduplicated;
- singular/plural summaries and prefix normalization are correct;
- OfficeCLI subprocess arguments match the selected local implementation;
- doctor covers missing, outdated, healthy, and malformed-version binaries;
- OfficeCLI validation errors prevent artifact and memory delivery; and
- QBR includes action items in its next-quarter plan.

Add a QBR golden case. Golden evaluation uses an injected deterministic in-memory
renderer so content scoring does not require OfficeCLI in CI; OfficeCLI adapter
behavior remains covered by adapter and doctor tests. Account Brief and Meeting
goldens will be updated only where the intentionally improved public output
changes their expectations.

The final verification matrix is Python 3.11 and 3.12, Ruff, the complete golden
evaluation directory, package build, wheel installation, CLI smoke tests, API
smoke tests, and a manual `office doctor` failure-path check when the binary is
not installed.

## Packaging and repository hygiene

- Add `build` to the development extra so the documented `python -m build`
  command succeeds after installing `.[dev]`.
- Replace the deprecated TestClient dependency with the compatible `httpx2`
  distribution and require warning-free test output.
- Correct project URLs to
  `https://github.com/karthiknambiar/csm-skills-framework`.
- Add the full Apache License 2.0 text as `LICENSE` and configure wheel license
  inclusion through project metadata.
- Add QBR evaluation coverage to CI.
- Keep the REST API unauthenticated by design and strengthen the embedding and
  deployment warning without adding an API-key requirement.

The pre-existing untracked `uv.lock` and usability-review directory are user
workspace state. They will not be staged or committed as part of this work.

## Secret prevention

The completed baseline scan found no high-confidence API keys or private keys in
the worktree, tracked files, or Git history. Hardening adds defense in depth:

- `.gitignore` excludes `.env`, `.env.*`, private-key files, and common local
  credential files while allowing a value-free `.env.example` if needed later;
- `scripts/check_secrets.py` scans the worktree, tracked files, and optionally
  every reachable Git revision for high-confidence provider key and private-key
  formats;
- findings report only path, line number, pattern category, and commit IDâ€”never
  the matched value;
- scanner tests construct dummy values at runtime so test fixtures do not look
  like live credentials in the repository; and
- CI performs a full-history checkout and runs the redacted scanner before tests.

Generic terms such as `api_key` in explanatory documentation are not failures.
Only credential-shaped values and private-key blocks fail the check.

## Error handling

All user-facing CLI failures follow the documented contract:

- exit `0`: success;
- exit `1`: valid evaluation completed but did not meet its threshold;
- exit `2`: validation, configuration, OfficeCLI, file-system, or secret-scan
  configuration failure.

CLI errors are concise and omit Python tracebacks. REST mappings remain `404`
for unknown skills, `422` for invalid contracts, and `502` for renderer/provider
execution failures. No exception message may include environment-variable values,
subprocess environment dumps, or matched secret text.

## Compatibility and migration

The runner's artifact handler is an optional keyword-only argument, so existing
Python callers remain source-compatible. Existing `--input` behavior remains.
Existing Account Brief fields remain, with defaulted additions only.

The new `ACTION_ITEM` memory kind and built-in skill metadata versions are public
minor changes under the pre-1.0 compatibility policy. Documentation will include
a migration note: consumers that previously treated all meeting commitments as
tasks should read both `action_item` and `commitment` during transition.

The old configurable OfficeCLI argument-template fields will be deprecated for
one minor release. Existing custom adapters may continue to inject any
`OfficeArtifactRenderer`; only the default `OfficeCLIArtifactRenderer` becomes
specific to the selected local binary.

## Acceptance criteria

The hardening release is complete when:

1. A failed CLI artifact destination leaves Customer Memory unchanged and emits
   no traceback.
2. A PowerShell user can run a generic skill using `--input-file` without special
   quoting.
3. Account Brief and Meeting outputs demonstrate the corrected taxonomy,
   deduplication, grammar, and recommendations on bundled dummy data.
4. `csaf office doctor` accurately distinguishes absent, outdated, broken, and
   healthy local OfficeCLI installations without modifying user documents.
5. QBR uses the selected fully local OfficeCLI contract and rejects invalid
   documents before committing memory.
6. QBR has a passing deterministic golden evaluation independent of OfficeCLI.
7. `python -m build` succeeds from the documented development environment, and
   the wheel contains the Apache-2.0 license.
8. Tests pass without the existing TestClient deprecation warning.
9. The redacted worktree, tracked-file, and Git-history secret scans report no
   credentials.
10. Python 3.11 and 3.12 tests, Ruff, golden evaluations, package build, wheel
    installation, CLI/API smoke tests, and final Git diff review pass.
