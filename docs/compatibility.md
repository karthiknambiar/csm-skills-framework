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
