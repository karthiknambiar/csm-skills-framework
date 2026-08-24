# OfficeCLI integration

QBR generation requires the fully local, deterministic [`iOfficeAI/OfficeCLI`](https://github.com/iOfficeAI/OfficeCLI) document engine. CSAF supports OfficeCLI 1.0.137 or newer and pins stable version 1.0.143 for native installation. This is the Apache-2.0 single-binary project, not a hosted document generator with a similar name.

No API key is required. CSAF does not call a hosted model, accept an Office or model-provider API key, or send QBR content to a hosted service. Account Brief and Meeting Copilot continue to work when OfficeCLI is absent.

## Native installation

The native installer includes the pinned OfficeCLI 1.0.143 binary in its displayed plan because OfficeCLI is mandatory for PowerPoint and Word QBR artifacts. It names the version, verified release asset, destination, and network use. It installs the binary only after explicit consent. The installer sets `OFFICECLI_SKIP_UPDATE=1`; OfficeCLI does not update itself inside CSAF.

Run native setup from the [installation guide](installation.md). The CSAF updater checks stable releases and notifies, but never auto-installs a CSAF or OfficeCLI update. An approved `csaf setup update` keeps OfficeCLI pinned to the version in that tagged CSAF manifest.

## Official manual install

Direct or manually managed CLI users can use OfficeCLI's [official manual install route for the stable v1.0.143 release](https://github.com/iOfficeAI/OfficeCLI/releases/tag/v1.0.143). Review the upstream asset and your organization's software policy, verify its published checksum, place `officecli` on `PATH`, and keep automatic updating disabled when CSAF runs it. Manual OfficeCLI installation is outside CSAF ownership, so `csaf setup uninstall --include-officecli` does not remove that binary.

To select an existing verified binary without changing `PATH`, set `CSAF_OFFICECLI` to its absolute path. Do not point CSAF at a hosted wrapper or an unreviewed moving build.

## Check readiness

Run the local diagnostic before the first QBR:

```bash
csaf office doctor
```

For automation, request its stable JSON report:

```bash
csaf office doctor --json
```

The doctor checks the executable, requires OfficeCLI 1.0.137 or newer, then creates and validates temporary PPTX and DOCX smoke artifacts. It reports each check as `pass`, `fail`, or `skip`, removes temporary files, and never reads or modifies user documents. Exit code `0` means QBR rendering is ready; exit code `2` means installation, version, or smoke rendering needs attention. Diagnostic paths and credential-shaped text are redacted.

Native users can check the whole installation with:

```bash
csaf setup doctor --json
```

QBR commands also run an executable-and-version preflight before reading Customer Memory or creating deliverable artifacts.

## Supported local command flow

For a new artifact, the adapter executes OfficeCLI with argument arrays and no shell interpolation:

```text
officecli create <working-file>
officecli batch <working-file> --input <batch-json> --json
officecli validate <working-file> --json
officecli view <working-file> issues --json
```

Mutation commands set `OFFICECLI_RESIDENT_FLUSH=each`, so persistence does not depend on an open resident. Work occurs in a private temporary directory with captured diagnostics and a timeout. A validation failure or structural error prevents artifact delivery and Customer Memory effects.

User templates, bundled templates, and existing documents are copied to the private working directory. The adapter applies its deterministic atomic batch to the copy and never changes the source file. PowerPoint and Word layout commands are generated locally from the same format-neutral QBR sections.

## Migration from configurable argument templates

Earlier CSAF previews exposed `create_arguments` and `update_arguments` on `OfficeCLIConfig`. These fields remain functional throughout CSAF 0.1.x for a one-minor compatibility window, but constructing a config with either field emits `DeprecationWarning`. They will be removed in CSAF 0.2.0.

Supplying either legacy field selects the deprecated renderer path. A missing counterpart uses its old default template, so applications can override only create or only update while migrating. Supported placeholders remain `{format}`, `{spec}`, `{output}`, `{template}`, and `{existing}`. Existing files and templates are copied into a private temporary directory before the legacy process runs; the user-owned source is never passed as a mutable working file. Legacy subprocess output must be strict UTF-8 and the process must create the requested output artifact.

The supported default is the local OfficeCLI `create`/`batch`/`validate`/`view` flow described above. New integrations should not adopt the legacy fields. Applications that need a different command surface should implement `OfficeArtifactRenderer` and inject the custom renderer with `create_runtime(office_renderer=renderer)`.
