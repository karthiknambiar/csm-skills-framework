# OfficeCLI integration

QBR generation requires the fully local, deterministic
[`iOfficeAI/OfficeCLI`](https://github.com/iOfficeAI/OfficeCLI) document engine,
version 1.0.137 or newer. This is the Apache-2.0 single-binary project, not the
unrelated hosted document generator with a similar name. CSAF does not call a
hosted model, does not accept an Office or model-provider API key, and does not
send QBR content to a hosted service.

OfficeCLI is mandatory for PowerPoint and Word QBR artifacts. Other CSAF skills
continue to work without it.

## Install OfficeCLI

Use the installation command published by the selected OfficeCLI project for
your platform:

```powershell
irm https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.ps1 | iex
```

```bash
curl -fsSL https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.sh | sh
```

Review the upstream script and your organization's software policy before
running either command. CSAF shows this official guidance but never installs or
updates OfficeCLI. After installation, ensure `officecli` is on `PATH`.

## Check readiness

Run the human-readable diagnostic before the first QBR:

```bash
csaf office doctor
```

For automation, request its stable JSON report:

```bash
csaf office doctor --json
```

The doctor checks the executable, requires OfficeCLI 1.0.137 or newer, then
creates and validates temporary PPTX and DOCX smoke artifacts. It reports each
check as `pass`, `fail`, or `skip`, removes its temporary files, and never reads
or modifies user documents. Exit code `0` means QBR rendering is ready; exit code
`2` means installation, version, or smoke rendering needs attention. Diagnostic
paths and credential-shaped text are redacted.

QBR commands also run a quick executable-and-version preflight before reading
customer memory or creating deliverable artifacts.

## Supported local command flow

For a new artifact, the adapter executes the selected OfficeCLI command surface
with argument arrays and no shell interpolation:

```text
officecli create <working-file>
officecli batch <working-file> --input <batch-json> --json
officecli validate <working-file> --json
officecli view <working-file> issues --json
```

Mutation commands set `OFFICECLI_RESIDENT_FLUSH=each`, so persistence is
immediate and does not depend on an open resident. Work occurs in a private
temporary directory with captured diagnostics and a timeout. Validation failure
or structural issues classified as errors prevent artifact delivery and Customer
Memory effects.

Templates and existing documents are copied to the private working directory;
the adapter applies its deterministic atomic batch to the copy and never changes
the source file. PowerPoint and Word layout commands are generated locally from
the same format-neutral QBR sections.

## Migration from configurable argument templates

Earlier CSAF previews documented `create_arguments` and `update_arguments` on
`OfficeCLIConfig`. Those argument-template fields are deprecated and are not
accepted by the selected default adapter. Remove them and use the supported
configuration fields only:

```python
from csaf.office import OfficeCLIConfig

config = OfficeCLIConfig(
    executable="officecli",
    timeout_seconds=120.0,
    minimum_version=(1, 0, 137),
)
```

Tests may use `prefix_arguments` to place a local test bridge before OfficeCLI
arguments. Applications that must support a different command surface should
implement `OfficeArtifactRenderer` and inject that renderer with
`create_runtime(office_renderer=renderer)` instead of rewriting default command
templates.