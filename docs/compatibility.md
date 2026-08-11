# Compatibility and versioning policy

CSAF is pre-1.0, so public contracts may evolve between minor releases. Changes
should still be deliberate, documented, and covered by migration notes.

## Public contracts

The following are treated as public once exported from a package `__init__.py`:

- Pydantic input, output, memory, connector, evaluation, and artifact models.
- `MemoryStore`, `Connector`, `Skill`, and renderer protocols.
- Built-in skill names and metadata.
- CLI command names, REST paths, and documented JSON shapes.
- Golden case schema and evaluation category names.

Private modules, underscored helpers, deterministic heuristics, and undocumented
implementation details may change without compatibility guarantees.

## Semantic versions

- **Patch:** compatible fixes, documentation, and additional tests.
- **Minor before 1.0:** new features and clearly documented contract changes.
- **Major after 1.0:** incompatible public-contract changes.

Skills and connectors carry their own semantic versions. Changing an input/output
schema, memory effect, or artifact contract requires at least a minor skill or
connector version change. Prompt versions are immutable.

## Deprecation

Where practical, mark a public contract deprecated for one minor release before
removal, document its replacement, and add a migration example. Security fixes
may require immediate removal or behavior changes.

Community extensions should declare the CSAF versions they test against and run
contract/conformance tests in CI. Vendor-specific payloads must not become shared
domain contracts merely to preserve an adapter implementation.

## Hardening-release migrations

### Meeting actions and commitments

Meeting Copilot 1.1.0 stores explicit tasks in the new `action_item` memory kind
and reserves `commitment` for promises. During migration, consumers that
previously treated every commitment as a task should read both `action_item` and
`commitment`, present them separately, and stop creating task records from
commitments once historical data has been reconciled. This enum and skill
metadata update is a pre-1.0 minor contract change.

### OfficeCLI configuration

The default renderer targets fully local `iOfficeAI/OfficeCLI` 1.0.137 or newer.
The configurable `create_arguments` and `update_arguments` argument-template
fields remain functional in CSAF 0.1.x for one minor compatibility window and
emit `DeprecationWarning` when used. If only one field is supplied, the other
operation uses its old default template. Both fields will be removed in CSAF
0.2.0. Migrate to the selected default command flow, or implement and inject a
custom `OfficeArtifactRenderer` when another command surface is required.

### Test client dependency

The development extra uses the `httpx2>=2,<3` distribution for Starlette's
TestClient compatibility and warning-free API tests. Development environments
created from an earlier CSAF preview should reinstall `.[dev]` so the old
`httpx` test-only distribution is replaced. This does not change CSAF's runtime
HTTP API or add an HTTP client runtime dependency.
