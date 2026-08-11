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

Earlier CSAF previews exposed `create_arguments` and `update_arguments` on
`OfficeCLIConfig`. These fields remain functional throughout CSAF 0.1.x for a
one-minor compatibility window, but constructing a config with either field
emits `DeprecationWarning`. They will be removed in CSAF 0.2.0.

Supplying either legacy field selects the deprecated renderer path. A missing
counterpart uses its old default template, so applications can override only
create or only update while migrating. The supported placeholders remain
`{format}`, `{spec}`, `{output}`, `{template}`, and `{existing}`. Existing files
and templates are copied into a private temporary directory before the legacy
process runs; the user-owned source is never passed as a mutable working file.
Legacy subprocess output must be strict UTF-8 and the process must create the
requested output artifact.

The selected default remains the local OfficeCLI 1.0.137
`create`/`batch`/`validate`/`view` flow described above. New integrations should
not adopt the legacy fields. Applications that need a different command surface
should implement `OfficeArtifactRenderer` and inject the custom renderer with
`create_runtime(office_renderer=renderer)`.
