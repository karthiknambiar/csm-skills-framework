# Contributing

Thank you for helping build CSAF.

## Development setup

CSAF requires Python 3.11 or newer. On Windows PowerShell, create an isolated
environment and install the package plus all development tools without creating
or updating a lock file:

```powershell
python -m venv .venv
uv pip install --python .\.venv\Scripts\python.exe -e ".[dev]"
```

On macOS or Linux:

```bash
python3 -m venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

The development extra includes pytest, Ruff, the compatible TestClient
transport, and the package build frontend.

## Local checks

Run tests, linting, a package build, and the redacted credential scanner before
submitting a change:

```bash
python -m pytest
ruff check .
python -m build
python scripts/check_secrets.py --worktree --tracked --history
```

The scanner reports only location and finding category, never matched values.
Do not commit `.env` files, private keys, local credential files, generated
artifacts, or build output. If a real credential is discovered, revoke it first,
then remove it from the worktree and Git history according to your
organization's incident process. Do not paste the value into an issue or pull
request.

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