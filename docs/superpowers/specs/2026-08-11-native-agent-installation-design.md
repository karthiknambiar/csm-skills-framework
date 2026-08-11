# CSAF Native Agent Installation Design

**Date:** 2026-08-11
**Status:** Approved for implementation planning
**Target:** Stable native distribution for Codex and Claude Code

## Purpose

Complete CSAF's original agent-facing product direction by packaging the existing
deterministic customer-success runtime as a native capability for Codex and
Claude Code. Non-technical users should be able to ask their assistant to install
CSAF from GitHub, while technical users should have a one-command installer.
Both paths must produce the same local, versioned, supportable installation.

The native assistant is the conversational layer. CSAF remains the deterministic
execution layer for Account Brief, Meeting Copilot, and QBR workflows. The local
`iOfficeAI/OfficeCLI` binary remains the mandatory document renderer. CSAF does
not call hosted AI services, request model-provider API keys, or upload customer
inputs or generated documents.

## Goals

1. Provide native CSAF integration for Codex and Claude Code from one release.
2. Support both a one-command installer and assistant-led GitHub installation.
3. Install into every detected supported assistant unless the user selects an
   explicit target override.
4. Bootstrap a private Python runtime so users do not need Python knowledge.
5. Install mandatory OfficeCLI only after clear, explicit consent.
6. Support Windows, macOS, and Linux from the first stable release.
7. Install the latest tagged stable CSAF release, never a moving `main` branch.
8. Check for stable updates automatically but only notify the user.
9. Preserve fully local, deterministic workflow execution after installation.
10. Support user-provided QBR templates and a vetted bundled generic template.
11. Replace the developer-heavy README with concise native installation, usage,
    diagnostics, update, and recovery guidance.

## Non-goals

- The installer will not silently install or update CSAF or OfficeCLI.
- The installer will not require users to create an API key.
- QBR generation will not search for or download arbitrary templates at runtime.
- CSAF will not replace the native permission or trust model of either assistant.
- The first release will not depend on acceptance into an official marketplace.
- The installer will not modify customer source documents or user templates.
- The project will not remove its CLI, SDK, or REST API; they remain supported
  execution and integration surfaces beneath the native assistant experience.

## Architecture

One tagged GitHub release will contain a shared version manifest, platform
installers, checksummed runtime assets, and two thin native adapters:

1. **Codex adapter:** a standard `csaf` skill with `SKILL.md`, Codex UI metadata,
   a launcher, and progressively disclosed workflow references.
2. **Claude Code adapter:** a versioned plugin with a `.claude-plugin` manifest,
   the equivalent `csaf` skill, and an entry in the repository's marketplace
   manifest.
3. **Shared local runtime:** one private per-user CSAF installation invoked by
   either adapter.
4. **Bootstrap layer:** platform scripts that install, repair, update, diagnose,
   and uninstall the shared runtime and native adapters.
5. **Release manifest:** the authoritative mapping of stable CSAF version,
   compatible OfficeCLI version, assets, sizes, and checksums.

The adapters contain conversational routing and safe invocation instructions,
not a duplicate CSAF implementation. Both call the same versioned launcher and
therefore observe the same data, diagnostics, behavior, and installed version.

Claude Code distribution follows its GitHub marketplace and versioned plugin
model. Codex distribution follows its native skill structure and installation
location. The common release does not require both products to use identical
packaging internals.

## Release contents

The repository will contain source-controlled native packaging and bootstrap
sources. Each stable GitHub release will publish platform-appropriate assets
similar to:

```text
csaf-release-manifest.json
csaf-runtime-<version>-py3-none-any.whl
csaf-codex-skill-<version>.zip
csaf-claude-plugin-<version>.zip
install.ps1
install.sh
SHA256SUMS
```

The Claude plugin source will include `.claude-plugin/plugin.json`, and the
repository will expose `.claude-plugin/marketplace.json`. Every manifest version
must match the Git tag and packaged adapter versions. Stable installers resolve
GitHub's latest stable release asset, not source archives from `main`.

## Installation experiences

### One-command installation

The README will show one PowerShell command for Windows and one shell command for
macOS/Linux. Before changing the system, the installer displays:

- the CSAF version;
- the mandatory OfficeCLI version and purpose;
- detected Codex and Claude Code installations;
- every assistant target that will receive the adapter;
- the destination directories; and
- the release source and network activity.

The user must confirm before installation proceeds. `--yes` enables unattended
installation while retaining checksum, compatibility, and diagnostic checks.
`--codex-only` and `--claude-only` restrict adapter targets. With no override,
the installer installs into every detected supported assistant.

The successful path is:

1. Detect platform and assistants.
2. Resolve and download the latest tagged stable manifest.
3. Display the complete consent summary.
4. Verify asset checksums.
5. Install the private CSAF runtime.
6. Install or validate mandatory OfficeCLI.
7. Install every selected native adapter.
8. Run diagnostics.
9. Activate the installation.
10. Print example natural-language requests.

### Assistant-led GitHub installation

A user may ask Codex or Claude Code to install CSAF from
`karthiknambiar/csm-skills-framework`. The product's native installer first adds
the adapter. If the shared runtime is absent, the adapter returns a structured
bootstrap request explaining that it will install CSAF and mandatory OfficeCLI
locally, requires network access for verified stable assets, and requires no API
key or hosted AI service.

After the user approves, the assistant invokes the same bootstrap layer used by
the one-command installer. The two entry points must produce the same state file,
versions, files, diagnostics, and update behavior.

## Consent model

Consent is required before the first material system change. The prompt must name
OfficeCLI explicitly and explain that QBR PowerPoint and Word generation cannot
work without it. Consent covers the exact displayed versions, destinations, and
assistant targets; it is not a standing approval for future updates.

Fresh consent is required when:

- installing CSAF or OfficeCLI for the first time;
- installing an adapter into another assistant;
- updating or reinstalling OfficeCLI;
- activating a new CSAF stable version; or
- uninstalling OfficeCLI or user data.

Read-only detection, version lookup, checksum verification, and diagnostics may
run before consent. The installer uses user-level locations and permissions by
default. It requests elevation only when an upstream OfficeCLI installation
method genuinely requires it and explains why immediately before the request.

## Local runtime and state

Default CSAF data roots are:

- Windows: `%LOCALAPPDATA%\CSAF`
- macOS: `~/Library/Application Support/CSAF`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/csaf`

The installer downloads a pinned, checksummed `uv` executable into the CSAF data
root. It uses that private tool to provision a compatible CPython runtime and
install the released CSAF wheel. The bootstrap tool and Python environment are
not added globally to `PATH`.

The logical runtime layout is:

```text
CSAF/
├── bin/
├── versions/
│   └── <version>/
├── current.json
├── state.json
└── update-cache.json
```

`state.json` records installed versions, verified asset checksums, selected
assistants, adapter locations, and whether CSAF installed OfficeCLI. It contains
no customer data or credentials. A launcher reads `current.json` to select the
active runtime. Installers stage and diagnose a new version before atomically
switching that pointer.

## Lifecycle commands

The bootstrap layer exposes idempotent operations:

- `install`: add missing components and leave healthy matching components intact.
- `repair`: verify recorded assets and reinstall only missing or damaged parts.
- `update`: show the proposed stable update and request consent before applying it.
- `doctor`: check runtime, OfficeCLI, adapters, permissions, and version compatibility.
- `uninstall`: remove CSAF runtime and assistant integrations after confirmation.
- `uninstall --include-officecli`: also remove OfficeCLI only when state records
  that CSAF installed it.

If no supported assistant is detected, the installer may install the runtime but
must report that no native adapter was added and print the later adapter-install
command. A repeated install converges on the same healthy state rather than
creating duplicate environments or plugin entries.

## Update behavior

The launcher checks for a newer tagged stable version at most once every 24
hours. It caches the timestamp and result in `update-cache.json`. Network failure
is non-fatal and never blocks normal local skill use.

When an update exists, the assistant or CLI displays the installed version, the
available version, and an explicit update command. The checker does not download
or install the update. Updates use the same consent, verification, staging,
diagnostic, and atomic activation flow as first installation.

Claude marketplace or assistant settings must not be used to bypass CSAF's
notify-only runtime policy. Adapter metadata may refresh independently, but a
new runtime is never activated without explicit user approval.

## Native skill behavior

The `csaf` skill activates for customer-success work involving account briefs,
meeting follow-up, QBR preparation, CSAF readiness, or OfficeCLI readiness. Its
main instructions remain concise and route to separate references:

```text
csaf/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── scripts/
│   └── csaf-launcher
└── references/
    ├── account-brief.md
    ├── meeting-copilot.md
    ├── qbr.md
    └── troubleshooting.md
```

The native skill may collect missing business information conversationally, but
CSAF validates inputs and executes the deterministic workflow. The assistant must
not invent required identifiers, silently alter customer records, claim an
artifact exists before delivery succeeds, or expose raw subprocess diagnostics.

For QBR generation, the adapter runs OfficeCLI preflight before processing
customer data. Generated artifacts go to a user-approved output directory, and
the assistant returns their exact local paths. If the runtime is absent or
unhealthy, the skill switches to consent-driven installation or repair guidance.

## QBR templates

Users may provide a PowerPoint template and, when desired, a Word template. CSAF
validates each source, copies it into a private working directory, and never
modifies the original. If only one template is supplied, the missing document
type uses the bundled generic counterpart.

If no user template is supplied, CSAF uses a polished generic QBR PowerPoint and
Word template set bundled in the stable release. The generic templates may be
sourced from the internet only during release preparation. Before inclusion they
must be:

- manually reviewed for layout and content suitability;
- covered by a compatible license with recorded provenance;
- malware-scanned;
- rendered and visually inspected;
- checksum-pinned; and
- included in release secret and package scans.

QBR generation never searches for or downloads a template dynamically. Template
changes ship only through tagged stable releases and follow the notify-only
update policy. The final response identifies whether CSAF used user templates or
the bundled generic templates.

## README and onboarding

`README.md` will become a concise, native-agent-first entry point rather than a
complete CLI, SDK, connector, and framework reference. Detailed material remains
in the existing `docs/` pages and is linked from the README instead of duplicated.

The README will use this order:

1. A short explanation of CSAF and its local deterministic architecture.
2. A prominent statement that OfficeCLI is mandatory for QBR document creation,
   is installed only with consent, and requires no API key.
3. A quick-install table with the supported Windows, macOS, and Linux commands.
4. An assistant-led GitHub installation prompt for Codex and Claude Code.
5. A first-use section with one natural-language example for Account Brief,
   Meeting Copilot, and QBR.
6. QBR template guidance covering user templates and the bundled vetted default.
7. A compact command table for `doctor`, `repair`, `update`, and `uninstall`.
8. A troubleshooting decision path for missing OfficeCLI, failed diagnostics,
   invalid templates, unavailable network, and no detected assistant.
9. A concise privacy and security statement: local execution, no hosted AI, no
   API keys, no customer-data upload, verified stable release assets.
10. Links to detailed CLI, SDK, REST, OfficeCLI, compatibility, tutorial,
    contributing, and security documentation.

Every published command must be exercised by documentation tests or an installer
smoke test. The README must clearly distinguish conversational usage inside a
native assistant from advanced direct CLI usage. It must not make users read
development setup instructions before reaching installation or first use.

Development, SDK, REST, connector, and evaluation details will be summarized in
one compact advanced-use section and linked to their authoritative documentation.
Repository layout and contributor setup will move out of the primary onboarding
flow so the most common install-to-first-result path stays short.

## Security and privacy

- Download only tagged release assets over HTTPS.
- Verify all assets before execution or installation.
- Use subprocess argument arrays and avoid shell interpolation for internal work.
- Use private staging and temporary directories with bounded cleanup.
- Sanitize paths, environment assignments, provider-shaped secrets, private-key
  material, stdout, and stderr before presenting diagnostics.
- Never request, store, or transmit model-provider API keys.
- Never upload customer inputs, memory data, templates, or generated documents.
- Record only installation metadata in bootstrap state.
- Preserve the native permission prompts and trust boundaries of Codex and
  Claude Code.
- Fail closed when a manifest, signature/checksum, version, template, or response
  contract is missing or malformed.

## Failure handling and recovery

Installation and update work is transactional. Assets are downloaded and
verified in a private staging directory. A new version becomes active only after
runtime, OfficeCLI, adapter, and smoke diagnostics pass. Failure removes
incomplete staged files and leaves the previous working version active.

Expected failure behavior:

- **No internet:** keep the current installation usable and explain how to retry.
- **Unsupported platform:** stop before changes and name the unsupported requirement.
- **OfficeCLI failure:** retain CSAF, mark document generation unavailable, and
  recommend `doctor` or `repair`.
- **No assistant detected:** retain the runtime and print adapter-install guidance.
- **Invalid user template:** preserve the source and offer the bundled template.
- **QBR rendering failure:** deliver no partial artifacts, report no success, and
  commit no related memory effects.
- **Update failure:** keep the prior active runtime.
- **Corrupted installation:** replace only components that fail recorded verification.

Diagnostics end with `ready`, `degraded`, or `failed` plus one recommended next
action. Public errors remain concise and sanitized.

## Testing strategy

### Runtime and installer TDD

Tests will be written before implementation for:

- platform and assistant detection;
- interactive consent and `--yes` behavior;
- manifest parsing, stable resolution, checksums, and version compatibility;
- clean install, repeat install, repair, update, rollback, and uninstall;
- Codex-only, Claude-only, both, and neither-detected environments;
- missing, outdated, malformed, failed, and healthy OfficeCLI installations;
- update-cache timing and non-fatal offline behavior;
- user templates, invalid templates, source preservation, and bundled fallback;
- transactional activation and cleanup after failures; and
- sanitized installer and runtime diagnostics.

CI will exercise installation on Windows, macOS, and Linux. Normal post-install
skill execution must pass offline. Real OfficeCLI smoke tests will generate,
validate, and inspect PowerPoint and Word artifacts.

### Native skill TDD

Skill authoring follows the `superpowers:writing-skills` RED-GREEN-REFACTOR
workflow. Realistic customer-success prompts first run against fresh agents
without the CSAF skill to record baseline failures. The minimal skill is then
written and the same prompts are repeated with each native adapter.

Scenarios cover workflow selection, missing information, consent, installation,
failed preflight, user templates, bundled-template fallback, artifact delivery,
privacy, and update notifications. Each adapter must be validated independently,
and the skill must be refined only in response to a demonstrated failure or
ambiguity.

## Release gates

A stable release requires all of the following:

1. Full Python test suite and Ruff checks.
2. Installer matrix on Windows, macOS, and Linux.
3. Codex skill validation and Claude plugin/marketplace validation.
4. Account Brief, Meeting Copilot, and QBR deterministic evaluations.
5. OfficeCLI doctor and real document-generation smoke tests.
6. User-template and bundled-template rendering with visual inspection.
7. Secret, malware, and packaged-asset scans.
8. Clean installation and upgrade from the prior stable release.
9. Offline execution after successful installation.
10. Final package, manifest, checksum, documentation, and Git diff review.

## Acceptance criteria

The native installation release is complete when:

1. A non-technical user can ask Codex or Claude Code to install CSAF from GitHub,
   approve the disclosed changes, and successfully run a customer-success skill.
2. A technical user can complete the same setup with one platform command.
3. A machine with both assistants receives both adapters and one shared runtime.
4. A machine without Python is bootstrapped without requiring package-management
   knowledge.
5. OfficeCLI installation is clearly disclosed, consented to, and diagnosed.
6. Repeated installation, repair, update, and rollback preserve a usable state.
7. Update checks notify about newer stable versions without installing them.
8. QBR generation works with user templates or vetted bundled generic templates
   without runtime template downloads.
9. Normal installed execution is local, deterministic, and requires no API key.
10. All release gates pass on Windows, macOS, and Linux.
11. A new user can find the correct platform install command, understand the
    OfficeCLI consent step, run one workflow, and locate `doctor` guidance from
    the README without consulting developer documentation.

## Implementation sequencing

Implementation planning should divide the work into independently testable
increments: release contracts and state model; portable bootstrap core; platform
installers; OfficeCLI consent/install integration; Codex adapter; Claude plugin
and marketplace; native skill behavior and forward tests; bundled QBR templates;
update/repair/uninstall flows; documentation; and cross-platform release gates.

No implementation begins until this written design has been reviewed and
approved by the user.
