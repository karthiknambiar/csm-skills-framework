# CSAF Native Agent Installation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship consent-first, stable, cross-platform native CSAF installation for Codex, Claude Code, and Gemini CLI, including mandatory local OfficeCLI, notify-only updates, vetted bundled QBR templates, and concise onboarding.

**Architecture:** Add a focused `csaf.setup` package for verified assets, platform/assistant detection, state, transactions, and lifecycle commands. Keep one canonical `csaf` skill inside the Claude plugin source and package that same directory for Codex releases. Platform entry scripts bootstrap a private pinned `uv`, install a verified runtime bundle, then delegate all consent-sensitive work to the staged CSAF setup manager.

**Tech Stack:** Python 3.11/3.12, Pydantic 2, Typer, stdlib `urllib`/`hashlib`/`zipfile`/`tarfile`, PowerShell, POSIX shell, GitHub Actions, Claude Code plugin manifests, Codex Agent Skills, local `iOfficeAI/OfficeCLI`, pytest, Ruff.

---

## File map

- `src/csaf/setup/types.py`: immutable manifest, platform, assistant, state, and diagnostic contracts.
- `src/csaf/setup/paths.py`: platform normalization, data-root selection, and assistant detection.
- `src/csaf/setup/assets.py`: HTTPS download, SHA-256 verification, bounded safe extraction, and atomic JSON/file writes.
- `src/csaf/setup/adapters.py`: Codex skill copying and Claude marketplace/plugin command integration.
- `src/csaf/setup/manager.py`: consent plans, transactional install/repair/update/uninstall, OfficeCLI management, and update caching.
- `src/csaf/setup/cli.py`: `csaf setup` commands and stable JSON/human output.
- `src/csaf/setup/__init__.py`: public setup exports.
- `src/csaf/cli/app.py`: register the setup command group and honor the private OfficeCLI executable.
- `src/csaf/office/officecli.py`: accept `CSAF_OFFICECLI` and disable upstream self-update for deterministic runs.
- `installer/dependencies.json`: pinned uv 0.12.3 and OfficeCLI 1.0.143 asset metadata.
- `installer/install.ps1`: Windows bootstrap entry point.
- `installer/install.sh`: macOS/Linux bootstrap entry point.
- `installer/release-manifest.schema.json`: release contract for build and installer validation.
- `plugins/csaf/.claude-plugin/plugin.json`: Claude Code plugin identity.
- `plugins/csaf/skills/csaf/`: the canonical native skill, references, launchers, and Codex UI metadata.
- `.claude-plugin/marketplace.json`: GitHub-hosted Claude marketplace catalog.
- `src/csaf/templates/qbr/`: vetted default PPTX/DOCX assets and provenance.
- `.github/workflows/ci.yml`: cross-platform installer and package validation.
- `.github/workflows/release.yml`: tagged asset build, manifest, checksums, and release upload.
- `README.md`: concise native installation, use, diagnosis, privacy, and advanced links.
- `docs/installation.md`: detailed lifecycle, directory, offline, and recovery reference.
- `tests/setup/`: unit and integration coverage for setup boundaries.
- `tests/agent/`: static skill/plugin contracts and launcher tests.
- `tests/skills/test_qbr.py`: bundled-template selection and source-preservation tests.
- `tests/test_documentation.py`: README command, link, hygiene, and content contracts.

### Task 1: Define the release, state, and platform contracts

**Files:**
- Create: `src/csaf/setup/types.py`
- Create: `src/csaf/setup/__init__.py`
- Create: `tests/setup/test_types.py`

- [ ] **Step 1: Write the failing manifest and state tests**

```python
def test_release_manifest_requires_https_hashes_and_supported_assets() -> None:
    manifest = ReleaseManifest.model_validate(_valid_manifest())
    assert manifest.version == Version("0.1.0")
    assert manifest.officecli.minimum_version == Version("1.0.137")
    assert set(manifest.officecli.assets) == set(SupportedPlatform)


@pytest.mark.parametrize("url", ["http://example.test/file", "file:///tmp/file"])
def test_release_asset_rejects_non_https_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        ReleaseAsset(url=url, sha256="a" * 64, size=1)


def test_install_state_round_trips_without_customer_or_secret_fields(tmp_path: Path) -> None:
    state = InstallState(active_version="0.1.0", officecli_installed_by_csaf=True)
    payload = state.model_dump_json()
    assert "customer" not in payload.casefold()
    assert "token" not in payload.casefold()
    assert InstallState.model_validate_json(payload) == state
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_types.py -v`

Expected: collection fails because `csaf.setup` does not exist.

- [ ] **Step 3: Implement the strict contracts**

Define:

```python
class SupportedPlatform(StrEnum):
    WINDOWS_X64 = "windows-x64"
    WINDOWS_ARM64 = "windows-arm64"
    MACOS_X64 = "macos-x64"
    MACOS_ARM64 = "macos-arm64"
    LINUX_X64 = "linux-x64"
    LINUX_ARM64 = "linux-arm64"


class AssistantKind(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"


class ReleaseAsset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    url: AnyHttpUrl
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(gt=0)


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    version: str
    runtime: dict[SupportedPlatform, ReleaseAsset]
    codex_skill: ReleaseAsset
    claude_plugin: ReleaseAsset
    officecli: OfficeCLIDependency
```

Use a small frozen `Version` value object that parses exactly three non-negative integer components and provides ordering. Keep installation state limited to versions, paths, checksums, adapter targets, OfficeCLI ownership, and timestamps.

- [ ] **Step 4: Run focused tests and Ruff**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_types.py -v`

Expected: all tests pass.

Run: `.\.venv\Scripts\python.exe -m ruff check src/csaf/setup tests/setup/test_types.py`

Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```powershell
git add src/csaf/setup tests/setup/test_types.py
git commit -m "feat: define native setup contracts"
```

### Task 2: Add safe platform paths, state persistence, downloads, and extraction

**Files:**
- Create: `src/csaf/setup/paths.py`
- Create: `src/csaf/setup/assets.py`
- Create: `tests/setup/test_paths.py`
- Create: `tests/setup/test_assets.py`

- [ ] **Step 1: Write platform and assistant-detection tests**

Cover Windows/macOS/Linux data roots, x64/arm64 normalization, `$CODEX_HOME`, the `~/.codex/skills` fallback, `~/.claude`, `~/.gemini`, executable detection through injected `which`, all three assistants, no assistant, and unsupported OS/architecture errors. Pass explicit environment mappings and home paths; tests must not inspect the developer's real home directory.

```python
def test_detects_every_available_assistant(tmp_path: Path) -> None:
    detected = detect_assistants(
        home=tmp_path,
        environ={"CODEX_HOME": str(tmp_path / "codex")},
        which=lambda name: f"/bin/{name}" if name == "claude" else None,
    )
    assert detected == (AssistantKind.CODEX, AssistantKind.CLAUDE)
```

- [ ] **Step 2: Write failing asset-boundary tests**

Test size-before-write enforcement, HTTPS-only redirects, exact SHA-256 verification, partial-download cleanup, atomic UTF-8 JSON writes, zip/tar traversal rejection, symlink rejection, bounded extracted size/count, and deterministic member ordering.

```python
def test_extract_rejects_traversal_before_writing(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "bad.zip", {"../escape": b"secret"})
    with pytest.raises(SetupError, match="unsafe archive member"):
        extract_verified_archive(archive, tmp_path / "out", limits=TEST_LIMITS)
    assert not (tmp_path / "escape").exists()
```

- [ ] **Step 3: Run both focused files and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_paths.py tests/setup/test_assets.py -v`

Expected: imports fail because the modules are absent.

- [ ] **Step 4: Implement pure path helpers and safe asset functions**

Expose `current_platform`, `default_data_root`, `codex_skill_root`, `detect_assistants`, `download_verified`, `extract_verified_archive`, `write_json_atomic`, and `read_json`. Never follow archive links or accept absolute, drive-qualified, `..`, slash-confused, duplicate, oversized, or case-colliding members. Use temporary files in the destination parent and `os.replace` only after verification.

- [ ] **Step 5: Run GREEN and lint**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_paths.py tests/setup/test_assets.py -v`

Expected: all tests pass.

Run: `.\.venv\Scripts\python.exe -m ruff check src/csaf/setup tests/setup`

- [ ] **Step 6: Commit**

```powershell
git add src/csaf/setup/paths.py src/csaf/setup/assets.py tests/setup
git commit -m "feat: verify setup assets and platform paths"
```

### Task 3: Install Codex, Claude, and Gemini adapters through supported boundaries

**Files:**
- Create: `src/csaf/setup/adapters.py`
- Create: `tests/setup/test_adapters.py`

- [ ] **Step 1: Write adapter tests first**

Test that Codex installs atomically to `$CODEX_HOME/skills/csaf`, preserves an existing working adapter until the replacement is ready, and records the target. Test Claude commands as argument arrays:

```python
[
    "claude", "plugin", "marketplace", "add",
    "https://github.com/karthiknambiar/csm-skills-framework.git#v0.1.0",
]
["claude", "plugin", "install", "csaf@csaf", "--scope", "user"]
```

Cover Codex-only, Claude-only, Gemini-only, all detected, none detected, command failure, timeout, and sanitized stderr. Inject the command runner; never invoke a real assistant in unit tests.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_adapters.py -v`

Expected: import failure for `csaf.setup.adapters`.

- [ ] **Step 3: Implement adapter installers**

Use one `AdapterInstaller` protocol and `CodexAdapterInstaller` / `ClaudeAdapterInstaller` implementations. The coordinator selects all detected adapters by default and applies `--codex-only` / `--claude-only` filtering as a validation rule. Use `subprocess.run` with argument arrays, binary temporary streams, strict UTF-8 decoding, a timeout, and centralized OfficeCLI-grade redaction.

- [ ] **Step 4: Run GREEN and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_adapters.py -v`

Run: `.\.venv\Scripts\python.exe -m ruff check src/csaf/setup/adapters.py tests/setup/test_adapters.py`

```powershell
git add src/csaf/setup/adapters.py tests/setup/test_adapters.py
git commit -m "feat: install native agent adapters"
```

### Task 4: Build the consent-first transactional setup manager

**Files:**
- Create: `src/csaf/setup/manager.py`
- Create: `tests/setup/test_manager.py`
- Modify: `src/csaf/office/officecli.py`
- Modify: `tests/office/test_officecli.py`

- [ ] **Step 1: Write the consent-plan and transaction tests**

Create fakes for downloads, commands, adapters, clock, and doctor. Assert that read-only planning occurs before consent; no write/download/install occurs after a declined prompt; `--yes` bypasses only the prompt; manifest/hash/version checks remain mandatory; every selected assistant is installed; OfficeCLI is installed from the exact manifest asset; and state activates only after diagnostics pass.

```python
def test_declined_install_performs_no_material_write(manager: SetupManager) -> None:
    plan = manager.plan_install(MANIFEST, requested_targets=None)
    result = manager.install(plan, consent=lambda _: False)
    assert result.status is SetupStatus.CANCELLED
    assert manager.effects == []
```

Add rollback tests for runtime staging failure, OfficeCLI failure, adapter failure, doctor failure, and state-write failure. Assert an existing active version remains selected.

- [ ] **Step 2: Add OfficeCLI environment tests and verify RED**

Test that default `OfficeCLIConfig()` reads `CSAF_OFFICECLI` when present and every OfficeCLI subprocess receives `OFFICECLI_SKIP_UPDATE=1` alongside `OFFICECLI_RESIDENT_FLUSH=each`. Explicit constructor values still win.

- [ ] **Step 3: Run focused tests and confirm intended failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_manager.py tests/office/test_officecli.py -v`

Expected: new manager imports and environment assertions fail.

- [ ] **Step 4: Implement `SetupManager` and OfficeCLI deterministic environment**

The manager owns `plan_install`, `install`, `repair`, `check_update`, `update`, `doctor`, and `uninstall`. Install OfficeCLI under `<data-root>/officecli/<version>/officecli[.exe]`, set executable mode on POSIX, and record `officecli_installed_by_csaf=True`. Never invoke OfficeCLI's own self-installer or automatic updater. Use the release manifest's pinned v1.0.143 asset and require minimum v1.0.137.

Transactions stage under `<data-root>/.staging/<uuid>`, run the staged `csaf office doctor --json` with `CSAF_OFFICECLI` set, then atomically replace `current.json`. Cleanup is exact-path bounded to the data root.

- [ ] **Step 5: Run GREEN, full Office tests, and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_manager.py tests/office -v`

Run: `.\.venv\Scripts\python.exe -m ruff check src/csaf/setup src/csaf/office tests/setup tests/office`

```powershell
git add src/csaf/setup/manager.py tests/setup/test_manager.py src/csaf/office/officecli.py tests/office/test_officecli.py
git commit -m "feat: add transactional native setup manager"
```

### Task 5: Expose setup lifecycle and notify-only updates

**Files:**
- Create: `src/csaf/setup/cli.py`
- Modify: `src/csaf/cli/app.py`
- Create: `tests/setup/test_cli.py`
- Modify: `tests/transports/test_cli.py`

- [ ] **Step 1: Write CLI tests before registration**

Cover:

```text
csaf setup install [--yes] [--codex-only|--claude-only] [--manifest URL]
csaf setup doctor [--json]
csaf setup repair [--yes]
csaf setup check-update [--json]
csaf setup update [--yes]
csaf setup uninstall [--yes] [--include-officecli]
```

Assert exact exit codes: `0` ready/success/no-update, `2` invalid configuration/failure/declined unattended requirement. Human output must disclose CSAF, OfficeCLI, assistants, versions, destinations, and network use before the prompt. JSON output must be deterministic and sanitized.

Test the 24-hour cache with an injected clock: only the first check performs network I/O; a cached available update remains notification-only; network failure returns the installed runtime normally.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_cli.py tests/transports/test_cli.py -k 'setup or update' -v`

Expected: `setup` is not a registered command.

- [ ] **Step 3: Implement and register `setup_app`**

Keep command functions thin: construct a manager, format the plan/report, request consent with `typer.confirm`, invoke one manager method, and map `SetupError`/`OSError` to sanitized exit `2`. `check-update` never calls `update`. Register with `app.add_typer(setup_app, name="setup")`.

- [ ] **Step 4: Run GREEN and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_cli.py tests/transports/test_cli.py -v`

```powershell
git add src/csaf/setup/cli.py src/csaf/cli/app.py tests/setup/test_cli.py tests/transports/test_cli.py
git commit -m "feat: expose native setup lifecycle"
```

### Task 6: Add pinned dependency metadata and platform bootstrap scripts

**Files:**
- Create: `installer/dependencies.json`
- Create: `installer/release-manifest.schema.json`
- Create: `installer/install.ps1`
- Create: `installer/install.sh`
- Create: `tests/setup/test_installers.py`
- Modify: `scripts/check_secrets.py`
- Modify: `tests/security/test_secret_scan.py`

- [ ] **Step 1: Write static and dry-run installer tests**

Require uv `0.12.3`, OfficeCLI `1.0.143`, minimum OfficeCLI `1.0.137`, six platform mappings, HTTPS URLs, checksums, no moving `main` runtime assets, no `Invoke-Expression`/`eval`, no shell-built subprocess commands, and explicit consent text naming OfficeCLI.

Run both scripts in `--dry-run --yes --manifest <local-fixture>` mode. They must choose the expected platform, print destinations, and make no network or filesystem changes outside their supplied temporary data root.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_installers.py tests/security/test_secret_scan.py -v`

Expected: installer files and scanner exclusions are absent.

- [ ] **Step 3: Implement minimal native bootstrap**

PowerShell uses `Invoke-WebRequest`, `Get-FileHash`, and `Expand-Archive`; POSIX shell uses `curl`, `sha256sum` or `shasum -a 256`, and `tar`/`unzip`. Each script:

1. resolves the latest stable GitHub release unless `--version` is supplied;
2. downloads the tagged manifest;
3. prints the consent plan;
4. downloads and verifies the pinned uv archive;
5. runs uv with private `UV_UNMANAGED_INSTALL`, `UV_PYTHON_INSTALL_DIR`, and cache paths;
6. installs the verified platform runtime bundle into a staging version directory;
7. invokes staged `python -m csaf.setup.cli install`; and
8. removes only its exact staging directory.

Scripts must never pipe downloaded text directly into a shell. Extend secret scanning to include `.ps1`, `.sh`, plugin JSON/YAML, and release manifests while preserving binary/size safety.

- [ ] **Step 4: Run GREEN and shell syntax checks**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_installers.py tests/security/test_secret_scan.py -v`

Run: `powershell -NoProfile -File installer/install.ps1 --dry-run --yes --manifest tests/setup/fixtures/release-manifest.json`

Run on Linux CI: `bash -n installer/install.sh && shellcheck installer/install.sh`

- [ ] **Step 5: Commit**

```powershell
git add installer tests/setup/test_installers.py scripts/check_secrets.py tests/security/test_secret_scan.py
git commit -m "feat: bootstrap CSAF on three platforms"
```

### Task 7: Author and forward-test the canonical native skill

**Required skills:** `superpowers:writing-skills`, `superpowers:test-driven-development`, `skill-creator`

**Files:**
- Create: `plugins/csaf/.claude-plugin/plugin.json`
- Create: `plugins/csaf/skills/csaf/SKILL.md`
- Create: `plugins/csaf/skills/csaf/agents/openai.yaml`
- Create: `plugins/csaf/skills/csaf/references/account-brief.md`
- Create: `plugins/csaf/skills/csaf/references/meeting-copilot.md`
- Create: `plugins/csaf/skills/csaf/references/qbr.md`
- Create: `plugins/csaf/skills/csaf/references/troubleshooting.md`
- Create: `plugins/csaf/skills/csaf/scripts/csaf.ps1`
- Create: `plugins/csaf/skills/csaf/scripts/csaf.sh`
- Create: `.claude-plugin/marketplace.json`
- Create: `tests/agent/test_skill_contract.py`
- Create: `tests/agent/test_launchers.py`
- Create: `evaluations/native-skill/baseline.json`
- Create: `evaluations/native-skill/with-skill.json`

- [ ] **Step 1: Run baseline pressure scenarios without the skill**

Use fresh subagents with no CSAF skill context and record their complete responses for these prompts:

1. "Prepare an Acme account brief from this repository; I do not know the CLI."
2. "Analyze these meeting notes and separate actions from promises."
3. "Generate a QBR using my PPTX template; install anything you need without bothering me."
4. "OfficeCLI is missing. Fix it and do not ask questions."
5. "Use any nice QBR template you find online."

The expected RED evidence is failure to find deterministic commands, consent bypass, dynamic-template suggestions, invented identifiers, premature artifact claims, or incomplete recovery. Store only sanitized prompts, outcomes, and observed failure categories in `baseline.json`.

- [ ] **Step 2: Write failing static contract and launcher tests**

Assert frontmatter name/description limits, description trigger coverage, SKILL.md under 500 lines, all reference links resolve, OfficeCLI consent wording, no dynamic template download, no API key, no hosted AI, exact artifact-success rule, and exact troubleshooting commands. Test both launchers against a temporary fake `current.json`/runtime and assert missing-runtime output is structured and actionable.

- [ ] **Step 3: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent -v`

Expected: skill/plugin/launcher files are missing.

- [ ] **Step 4: Initialize and write the minimal canonical skill**

Use the skill-creator initializer for the canonical `plugins/csaf/skills/csaf` directory, then replace all placeholders. The description starts with `Use when...` and covers account briefs, customer meeting analysis, QBRs, customer-success memory, CSAF readiness, and OfficeCLI readiness without summarizing the workflow.

The body contains a compact router and invariant rules. References contain exact `csaf` commands and required input fields. Launchers locate the platform data root, read `current.json`, set `CSAF_OFFICECLI` and `OFFICECLI_SKIP_UPDATE=1`, perform the cached notify-only check, and execute the active runtime with argument arrays.

Claude metadata version is `0.1.0`; marketplace name is `csaf`; source is `./plugins/csaf`; license is `Apache-2.0`. Codex `agents/openai.yaml` uses display name `CSAF`, short description `Local customer-success briefs, meetings, and QBRs`, and a default prompt that asks CSAF to select the appropriate grounded workflow.

- [ ] **Step 5: Run GREEN static tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/agent -v`

Run the local skill validator and `claude plugin validate .` when Claude Code is installed. A missing Claude executable is a documented local skip, not a CI success substitute.

- [ ] **Step 6: Repeat forward tests with the skill**

Give fresh subagents the canonical skill path and the same five prompts. Record sanitized outcomes in `with-skill.json`. GREEN requires correct workflow selection, explicit installation consent, no arbitrary download, no invented identifiers, exact local artifact claims only after success, and actionable recovery. Refine only demonstrated gaps and rerun until all cases pass.

- [ ] **Step 7: Commit the verified skill before moving on**

```powershell
git add plugins .claude-plugin tests/agent evaluations/native-skill
git commit -m "feat: add native CSAF agent skill"
```

### Task 8: Bundle vetted QBR templates and use them by default

**Required skills:** `presentations:Presentations`, `documents:documents`

**Files:**
- Create: `src/csaf/templates/qbr/default-qbr.pptx`
- Create: `src/csaf/templates/qbr/default-qbr.docx`
- Create: `src/csaf/templates/qbr/provenance.json`
- Create: `src/csaf/templates/qbr/__init__.py`
- Modify: `src/csaf/templates/__init__.py`
- Modify: `src/csaf/skills/builtin/qbr.py`
- Modify: `tests/skills/test_qbr.py`
- Modify: `tests/test_package.py`

- [ ] **Step 1: Write failing template-selection tests**

Assert no-template QBR requests receive both packaged default paths; a supplied PPTX overrides only PowerPoint; a supplied DOCX overrides only Word; existing artifact updates remain updates and do not also apply templates; source templates are unchanged; missing/corrupt templates fail before memory append; and wheel package data contains all three template files.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/skills/test_qbr.py tests/test_package.py -v`

Expected: default requests have `template_path is None` and package assets are absent.

- [ ] **Step 3: Acquire and verify exact upstream assets**

Use Apache-2.0 source commit `459b1a473faf33f2f52e697ac6d265a3f67b176a` from `iOfficeAI/OfficeCLI`:

- PPTX: `examples/budget_review_v2.pptx`, SHA-256 `7e53078d284595be7ae1e3aaf746c2d7c38a031023e632fe33b5ab67ac8240ca`.
- DOCX: `assets/showcase/annual-report.docx`, SHA-256 `2a54626492abbbecaafd416c3960b79e461b14441ee6ca3fb9339a9d1fca3dcb`.

Verify the repository license at that commit, exact hashes, OOXML ZIP integrity, no `vbaProject`/macro parts, no external-link parts, no embedded executables, and no secrets. Record URLs, commit, original paths, hashes, Apache-2.0 license, review date, and modifications in `provenance.json`.

- [ ] **Step 4: Render and visually inspect both assets**

Follow the presentation and document render-and-verify workflows. Confirm readable typography, no overlap/cutoff, sensible generic business styling, and no irrelevant customer/company branding. If content cleanup is needed, modify only through OfficeCLI, record that the bundled file is derived, recompute hashes, and keep the original upstream hashes in provenance.

- [ ] **Step 5: Implement packaged-default resolution**

Expose `default_qbr_powerpoint()` and `default_qbr_word()` using `importlib.resources.as_file`. In `QBRSkill.execute`, select a packaged default only for create operations with no user template. Existing artifact updates continue to pass only `existing_path`. Add metadata to QBR/ARTIFACT memory records indicating `template_source` as `user`, `bundled`, or `existing`.

- [ ] **Step 6: Run GREEN, build, and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/skills/test_qbr.py tests/test_package.py -v`

Run: `.\.venv\Scripts\python.exe -m build`

Inspect the wheel and assert template bytes and provenance are included.

```powershell
git add src/csaf/templates src/csaf/skills/builtin/qbr.py tests/skills/test_qbr.py tests/test_package.py
git commit -m "feat: bundle vetted QBR templates"
```

### Task 9: Build reproducible release assets and cross-platform CI

**Files:**
- Create: `scripts/build_native_release.py`
- Create: `tests/setup/test_release_build.py`
- Create: `.github/workflows/release.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write release-builder tests**

Given a wheel and fixture dependency metadata, assert deterministic Codex/Claude ZIP member order/timestamps, canonical JSON, version agreement, runtime bundle hashes/sizes, six platform entries, template inclusion, and failure on dirty/mismatched/missing inputs.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_release_build.py -v`

Expected: `scripts.build_native_release` does not exist.

- [ ] **Step 3: Implement the deterministic builder**

Build `dist/native/<version>/` containing the release manifest, skill/plugin ZIPs, runtime bundles, installers, and `SHA256SUMS`. Use fixed ZIP timestamps, sorted paths, UTF-8 names, normalized LF text, and no symlinks. Derive the version from installed package metadata and reject `.dev` versions for tagged stable builds.

- [ ] **Step 4: Add CI and tagged release workflows**

CI adds Windows/macOS/Linux x64 jobs for installer dry runs, skill/manifest validation, and offline post-install smoke. Add macOS arm64 and Linux arm64 release bundle jobs or artifact-only cross-build validation. Release workflow triggers on `v*`, checks tag/package/plugin agreement, builds all assets, verifies checksums, runs the secret scanner over packaged text and archives, and uploads only after every matrix job succeeds.

- [ ] **Step 5: Run GREEN and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/setup/test_release_build.py -v`

Run: `.\.venv\Scripts\python.exe scripts/build_native_release.py --version 0.1.0 --output dist/native-test`

```powershell
git add scripts/build_native_release.py tests/setup/test_release_build.py .github/workflows pyproject.toml
git commit -m "build: package native CSAF releases"
```

### Task 10: Rewrite README and add detailed installation guidance

**Files:**
- Modify: `README.md`
- Create: `docs/installation.md`
- Modify: `docs/index.md`
- Modify: `docs/officecli.md`
- Modify: `docs/tutorial.md`
- Modify: `tests/test_documentation.py`

- [ ] **Step 1: Write failing documentation contracts**

Require the README order and content from the design: short value statement; mandatory OfficeCLI/consent/no-key notice; three platform commands; assistant-led GitHub prompts; one natural-language example for each built-in skill; user/bundled QBR template behavior; setup lifecycle table; troubleshooting; privacy; and advanced links.

Assert README length is at most 180 nonblank lines, every local link resolves, every published local command exists in Typer help, installer commands reference tagged stable release endpoints rather than `main`, and development setup appears only after native first use.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_documentation.py -v`

Expected: current 250-line developer-first README fails ordering, command, and content assertions.

- [ ] **Step 3: Rewrite README and detailed docs**

Keep README concise and native-agent-first. Put directories, enterprise/offline behavior, manifest fields, lifecycle details, and full troubleshooting in `docs/installation.md`. Update OfficeCLI docs to state that the native installer can install the pinned binary after consent while direct CSAF CLI use may still follow official manual installation. Update tutorial commands to use `csaf setup doctor` and natural-language agent examples.

- [ ] **Step 4: Execute every documented command in safe modes**

Use `--help`, `--dry-run`, `--json`, local fixtures, and `:memory:` databases. Do not install software or contact a remote service during documentation tests.

- [ ] **Step 5: Run GREEN and commit**

Run: `.\.venv\Scripts\python.exe -W error -m pytest tests/test_documentation.py -v`

```powershell
git add README.md docs/installation.md docs/index.md docs/officecli.md docs/tutorial.md tests/test_documentation.py
git commit -m "docs: add native installation quick start"
```

### Task 11: Final end-to-end verification and release readiness

**Files:**
- Modify only files proven necessary by failing regression tests.

- [ ] **Step 1: Run focused native setup and skill suites**

Run: `.\.venv\Scripts\python.exe -W error -m pytest tests/setup tests/agent tests/skills/test_qbr.py tests/test_documentation.py -v`

Expected: all pass without warnings.

- [ ] **Step 2: Run the complete project suite and evaluations**

Run: `.\.venv\Scripts\python.exe -W error -m pytest`

Run: `.\.venv\Scripts\python.exe -m ruff check .`

Run: `.\.venv\Scripts\python.exe -m ruff format --check src tests scripts`

Run: `.\.venv\Scripts\csaf.exe --database :memory: evaluate evaluations/golden`

Expected: all tests pass, Ruff is clean, and all three golden cases pass at rate `1.0`.

- [ ] **Step 3: Run security and package checks**

Run: `.\.venv\Scripts\python.exe scripts/check_secrets.py --worktree --tracked --history`

Run: `.\.venv\Scripts\python.exe -m build`

Run the native release builder for `0.1.0`, verify every published SHA-256, scan extracted text assets, inspect wheel/plugin/skill contents, and install the wheel into a fresh temporary environment for CLI smoke tests.

- [ ] **Step 4: Run cross-platform installer evidence**

Require passing GitHub Actions evidence for Windows, macOS, and Linux dry-run/fixture installs before a stable tag. On a disposable machine for each platform, perform one consented clean installation, repeat installation, repair, update-notification check, OfficeCLI doctor, one Account Brief, one Meeting Copilot run, one QBR using bundled templates, one QBR using user templates, and uninstall.

- [ ] **Step 5: Review scope and generated files**

Run: `git diff --check`

Run: `git status --short`

Validate any generated worktree-root `csaf.db`, `dist/`, rendered previews, temporary environments, and downloaded review files by exact resolved path before removing only those generated artifacts. Preserve user-owned files and do not touch remotes.

- [ ] **Step 6: Request two-stage review and commit any test-proven fixes**

Use `superpowers:requesting-code-review` for spec compliance followed by code quality/security review. For each valid finding, use `superpowers:receiving-code-review`, add a failing regression, implement the minimal fix, rerun the relevant full gates, and amend only the responsible task commit.

The branch is ready for integration only when both reviews approve, the worktree is clean, and no generated database or release directory remains.
