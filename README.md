# Customer Success Agent Framework (CSAF)

CSAF turns local customer context into grounded account briefs, meeting follow-ups, and QBR documents from Codex, Claude Code, or Gemini CLI.

> **Testing status:** CSAF’s native agent integration is still being tested. Use non-production data, review each setup plan before consenting, and report unexpected behavior.

> **OfficeCLI is mandatory for QBR PowerPoint and Word output.** Native setup explains every destination and network action, then asks for explicit consent before it installs the stable pinned OfficeCLI 1.0.143 binary. No API key is required. Account Brief and Meeting Copilot work without OfficeCLI.

## Install

The commands below download the installer from the tagged stable `v0.1.0` release. Read the script before running it. The installer detects Codex, Claude Code, and Gemini CLI and installs one target for each detected supported assistant type for the current user/configured environment. It makes no change until it shows the plan and you consent.

### Windows

```powershell
$installer = Join-Path $env:TEMP "csaf-install.ps1"
Invoke-WebRequest "https://github.com/karthiknambiar/csm-skills-framework/releases/download/v0.1.0/install.ps1" -OutFile $installer
& $installer -Version "0.1.0"
```

### macOS

```bash
curl -fsSL "https://github.com/karthiknambiar/csm-skills-framework/releases/download/v0.1.0/install.sh" -o /tmp/csaf-install.sh
sh /tmp/csaf-install.sh --version 0.1.0
```

### Linux

```bash
curl -fsSL "https://github.com/karthiknambiar/csm-skills-framework/releases/download/v0.1.0/install.sh" -o /tmp/csaf-install.sh
sh /tmp/csaf-install.sh --version 0.1.0
```

Run the readiness check after installation:

```bash
csaf setup doctor
```

See [installation](docs/installation.md) for directories, offline setup, diagnostics, and updates.

## Ask your assistant to install CSAF

If you would rather not use a terminal, give Codex, Claude Code, or Gemini CLI this prompt:

> Open the CSAF GitHub release at `https://github.com/karthiknambiar/csm-skills-framework/releases/tag/v0.1.0`. Read the installation guide. Use the CSAF installer and release manifest from the tagged `v0.1.0` release. Permit only the manifest-declared CSAF and OfficeCLI assets, the tagged installer's pinned, checksum-verified uv 0.12.3 platform asset, and the uv-managed Python 3.12.13 dependency download. Do not download anything else. Install one target for each detected supported assistant type for the current user/configured environment. Show me the complete plan first. Ask for my consent before installing OfficeCLI or making any change.

For a check before installation, ask:

> Inspect the tagged CSAF v0.1.0 installer and tell me which files, assistants, network downloads, and local directories it would use. Do not install anything.

## Use CSAF in natural language

Your assistant selects the local CSAF workflow and asks for missing identifiers, input paths, output locations, and approval for memory changes.

### Account Brief

> "Prepare an Account Brief for customer `acme` using the last 90 days of Customer Memory, and save it to `acme-brief.md`."

Account Brief reports missing context instead of inventing it and cites the Customer Memory records it uses.

### Meeting Copilot

> "Use Meeting Copilot to analyze `acme-meeting.md` for customer `acme`, meeting `kickoff-42`. Separate actions from commitments and draft the follow-up."

Meeting Copilot preserves transcript evidence and records `action_item`, `commitment`, risk, timeline, and `feature_request` context.

### QBR

> "Create the 2026-Q3 QBR for customer `acme` in `artifacts/`. Use my PowerPoint template `brand-qbr.pptx` and the bundled Word template."

You may provide your own PowerPoint and Word QBR templates. For either format you do not supply, CSAF uses its sourced, vetted, bundled generic QBR templates. CSAF never downloads a template at runtime.

## Setup lifecycle

| Need | Command | What it does |
|---|---|---|
| Install | `csaf setup install` | Shows the plan and requests consent before installing |
| Check | `csaf setup doctor --json` | Reports runtime, OfficeCLI, adapter, and permission health |
| Repair | `csaf setup repair` | Repairs missing or damaged owned components after consent |
| Check updates | `csaf setup check-update --json` | Checks at most once per 24 hours and only notifies |
| Update | `csaf setup update` | Applies a tagged stable release after fresh consent |
| Remove | `csaf setup uninstall` | Removes CSAF-owned runtime and adapters after consent |

The updater never auto-installs updates. Use `csaf setup uninstall --include-officecli` only when you also want to remove the OfficeCLI binary installed by CSAF.

## Troubleshooting

Start with:

```bash
csaf setup doctor
csaf office doctor --json
```

If a component is missing or damaged, run `csaf setup repair` and review its plan. If no assistant was detected during installation, install Codex, Claude Code, or Gemini CLI first, then repair. Full diagnostics and recovery steps are in [installation](docs/installation.md#troubleshooting).

## Privacy

The CSAF runtime itself processes Customer Memory, templates, and generated artifacts locally and does not send them to a hosted AI service. Codex, Claude Code, or Gemini CLI may handle prompts and files under its provider and organization settings. Review your assistant provider's data controls before supplying customer information. Setup uses HTTPS only to obtain tagged release metadata and checksum-verified assets; `check-update` may contact the release endpoint, is cached for 24 hours, and does not install. Keep output directories and local database files within your organization's approved storage.

## Advanced documentation

- [Documentation index](docs/index.md)
- [Native installation and updates](docs/installation.md)
- [Tutorial](docs/tutorial.md)
- [OfficeCLI integration](docs/officecli.md)
- [CLI reference](docs/cli.md)
- [Architecture](docs/architecture.md)
- [Skill development](docs/skill-development.md)
- [Connector development](docs/connector-development.md)
- [Evaluation framework](docs/evaluations.md)

## Development

CSAF requires Python 3.11 or newer. Development setup is separate from native installation:

```bash
python -m venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest
```

On Windows, replace `.venv/bin/python` with `.\.venv\Scripts\python.exe`. See [Contributing](CONTRIBUTING.md) for the full test, lint, build, and secret-scanning workflow. Built-in Account Brief, Meeting Copilot, and QBR regressions run through the [evaluation framework](docs/evaluations.md).
