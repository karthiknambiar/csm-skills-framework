# Contributing

Thank you for helping build CSAF.

## Local checks

Create a Python 3.11+ environment, install the development extras, and run:

```bash
ruff check .
pytest
python -m build
```

## Change guidelines

1. Keep changes within the active milestone described in
   [`docs/milestones.md`](docs/milestones.md).
2. Respect the dependency rules in [`docs/architecture.md`](docs/architecture.md).
3. Add tests for behavior and documentation for public contracts.
4. Preserve history and provenance in all Customer Memory changes.
5. Keep vendor-specific behavior behind an adapter.
6. Use focused commits and explain architectural decisions in the pull request.

Runtime dependencies should be added only when used. Prefer protocols and
dependency injection over module-level clients or configuration.

## Extension contributions

- New skills must include typed inputs/outputs, declared memory effects and
  artifacts, lifecycle tests, documentation, and at least one golden case.
- New connectors must keep vendor payloads at the adapter boundary and include
  authentication, pagination, normalization, provenance, and checkpoint tests.
- New artifact renderers must implement the public renderer protocol and test
  create/update failure behavior without requiring network access.
- Public-contract changes must follow the
  [compatibility policy](docs/compatibility.md) and include migration guidance.

Use the [documentation index](docs/index.md) to locate the relevant development
guide before starting an extension.
