# Install CSAF

Native installation supports Windows, macOS, and Linux on x64 and arm64. It installs the stable CSAF runtime, the pinned OfficeCLI 1.0.143 binary, and one adapter target for each detected supported assistant type for the current user/configured environment. OfficeCLI is mandatory for QBR documents. The installer shows versions, destinations, assistants, and network use, then requires explicit consent before it changes the machine.

Use the tagged commands in the [README](../README.md#install). Do not substitute a `main` branch URL. Tagged installers verify the release manifest, asset size, and SHA-256 checksum before activation.

## Installation directories

The data root contains versioned runtimes, the CSAF-owned OfficeCLI binary, adapter receipts, update state, and private bootstrap files.

| Platform | Default data root |
|---|---|
| Windows | `%LOCALAPPDATA%\CSAF` |
| macOS | `~/Library/Application Support/CSAF` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/csaf`, normally `~/.local/share/csaf` |

Set `CSAF_DATA_ROOT` to an absolute, non-root path before setup to use another data root. Codex receives the `csaf` skill under `~/.codex/skills` or `$CODEX_HOME/skills`. Claude Code receives the user-scoped `csaf@csaf` plugin through its plugin manager; CSAF keeps its Claude adapter receipt under the data root. Setup does not install a Gemini CLI adapter.

## What setup does

1. Resolve a tagged stable release manifest and select the host platform.
2. Detect Codex and Claude Code. By default, select one target per detected assistant type for the current user/configured environment.
3. Show the CSAF version, pinned OfficeCLI version, destinations, assistant targets, downloads, and requested action.
4. Wait for explicit consent. `--yes` is consent for an already-reviewed displayed plan; do not use it when the plan has not been reviewed.
5. Download only HTTPS assets named in the manifest, verify size and SHA-256, and stage them below the data root.
6. Run local OfficeCLI readiness checks, activate the version atomically, install the assistant adapters, and record owned components.

No API key is required. Normal skill execution is local and does not call a hosted AI service.

## Manifest and offline installation

The release manifest is canonical JSON with a schema version, CSAF version, six platform runtime assets, Codex and Claude adapter assets, and OfficeCLI 1.0.143 assets with minimum supported version 1.0.137. Every asset entry supplies an HTTPS URL, exact byte size, and SHA-256 digest.

For a restricted or air-gapped network, publish the tagged installer and every release asset on an internal HTTPS mirror that the target machine can reach. Regenerate and review the manifest so each mirrored URL, size, and digest matches the approved asset. Asset URLs must remain HTTPS; `file:` URLs and local asset paths are not supported. You may pass the regenerated manifest itself as a local file:

```powershell
.\install.ps1 -DryRun -AssumeYes -ManifestPath C:\approved\csaf-release-manifest.json -DataRoot C:\approved\csaf-data
```

```bash
sh ./install.sh --dry-run --yes --manifest /approved/csaf-release-manifest.json --data-root /approved/csaf-data
```

`--dry-run` prints the selected plan without downloading assets or changing assistant and data directories. A local manifest does not make its assets local: a real installation still downloads each asset from the HTTPS URL recorded in that manifest. A completely disconnected install from copied local asset files is not supported. The setup CLI also accepts a reviewed local manifest with `csaf setup install --manifest PATH`.

## Setup lifecycle

| Action | Command | Consent and network behavior |
|---|---|---|
| Install | `csaf setup install` | Shows a plan; downloads and activates only after consent |
| Diagnose | `csaf setup doctor --json` | Reads local state and runs local health checks |
| Repair | `csaf setup repair` | Shows a repair plan and requires consent |
| Check | `csaf setup check-update --json` | Checks the stable release endpoint at most once per 24 hours and notifies |
| Update | `csaf setup update` | Downloads and applies a tagged stable release after fresh consent |
| Uninstall | `csaf setup uninstall` | Shows owned targets and requires consent before removal |

The updater automatically checks when the native launcher starts, caches the result for 24 hours, and notifies when a newer stable release is available. It never auto-installs updates. Run `csaf setup update` yourself and approve its plan. A failed update check does not stop the installed runtime.

`csaf setup uninstall` retains OfficeCLI by default. Add `--include-officecli` to remove it only when CSAF installed and recorded ownership of that binary. User-installed OfficeCLI and unrelated assistant files are not removed.

## Troubleshooting

Run the human-readable diagnostic first:

```bash
csaf setup doctor
```

For a machine-readable, sanitized report:

```bash
csaf setup doctor --json
csaf office doctor --json
```

Exit code `0` means the requested diagnostic is ready. Exit code `2` means setup, OfficeCLI, an adapter, permissions, or a smoke render needs attention.

If setup reports no assistants, install or open Codex or Claude Code and run `csaf setup repair`. Use `csaf setup install --codex-only` or `csaf setup install --claude-only` only when you intentionally want one detected assistant type.

If files are missing, checksums differ, or an adapter is incomplete, run:

```bash
csaf setup repair
```

Review the plan before consenting. Repair operates only on missing or damaged CSAF-owned components. If OfficeCLI alone fails, see [OfficeCLI diagnostics](officecli.md#check-readiness).

For update status without applying it:

```bash
csaf setup check-update --json
```

The report can indicate an available release, a cached result, or offline operation. None of those states starts an update.

## Updates

Stable updates come from versioned GitHub release manifests. Preview builds and a moving source branch are not update sources. Before updating, close concurrent CSAF work that writes artifacts or Customer Memory, run `csaf setup doctor`, then run:

```bash
csaf setup update
```

Read the displayed versions, assets, destinations, and network actions. Declining leaves the active version unchanged. After an approved update, run `csaf setup doctor` again. If activation is interrupted, `csaf setup repair` uses recorded state to restore or complete owned components.
