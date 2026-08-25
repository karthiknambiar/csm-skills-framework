# CSAF v0.1.0 Prerelease Design

**Date:** 2026-08-25
**Status:** Approved for implementation

## Goal

Publish the current `0.1.0` package as an installable GitHub prerelease so users can test CSAF on Codex, Claude Code, and Gemini CLI without presenting the native integration as a stable release.

## Release semantics

- Create one annotated version tag, `v0.1.0`, at the exact verified `main` commit.
- Publish the tag as a GitHub prerelease and do not mark it as the repository's latest stable release.
- Keep the existing direct, versioned installer URLs working. Users may install and test `v0.1.0` by following the README commands.
- Update the README's installation wording from "tagged stable" to "tagged testing prerelease" without changing the direct `v0.1.0` URLs or commands.
- Keep stable-release discovery separate. GitHub's latest-stable endpoint and CSAF's notify-only stable update checks must not select this prerelease.
- Put the testing warning, consent-first installation behavior, mandatory pinned OfficeCLI disclosure, and supported assistants in the release notes.

## Publication workflow

The tag-triggered GitHub Actions workflow remains the only release-asset producer. Before tagging, update its publication step to set prerelease metadata explicitly, disable latest-release promotion, and publish the approved testing notice as release notes. This avoids a window in which the testing build could appear stable.

The workflow must continue to:

1. Require agreement among the tag, Python package, Claude plugin, and marketplace versions.
2. Build the native release deterministically from the tagged commit.
3. Verify the manifest, package contents, immutable wheel closure, and `SHA256SUMS`.
4. Scan tracked history and packaged archives for secrets.
5. Validate all six cross-built platform runtime bundles.
6. Run consented READY installation smokes on Linux x64, macOS ARM64, and Windows x64 with external network access blocked after pinned assets are staged.
7. Upload assets only after every required job succeeds.

## Published assets

The prerelease must contain exactly the files produced by the native release builder:

- `install.ps1`
- `install.sh`
- `csaf-release-manifest.json`
- `SHA256SUMS`
- `csaf-codex-skill-0.1.0.zip`
- `csaf-claude-plugin-0.1.0.zip`
- `csaf-runtime-linux-arm64-0.1.0.zip`
- `csaf-runtime-linux-x64-0.1.0.zip`
- `csaf-runtime-macos-arm64-0.1.0.zip`
- `csaf-runtime-macos-x64-0.1.0.zip`
- `csaf-runtime-windows-arm64-0.1.0.zip`
- `csaf-runtime-windows-x64-0.1.0.zip`

The standalone Python wheel and source distribution are build-verification outputs, not separate GitHub release assets, because each native runtime bundle already contains the exact wheel and its complete hashed offline dependency closure.

## Failure handling

- Do not create or push the tag until the workflow-metadata change is committed, pushed to `main`, and its CI run is green.
- If the tag workflow fails before publication, keep the tag for diagnosis but do not manually upload partial or locally assembled assets.
- If publication metadata or the asset set is wrong, correct the workflow and rerun it against the same immutable tag only when the release has not been consumed; otherwise publish a new patch version rather than moving the tag.
- Never force-update or silently retarget the public version tag.

## Verification and acceptance

Before tag creation:

- Run documentation regressions proving the README identifies `v0.1.0` as an installable testing prerelease and retains the direct versioned installer commands.
- Run the full test suite with warnings treated as errors.
- Run Ruff lint and formatting checks.
- Build the wheel and source distribution.
- Run worktree, tracked-file, history, and package secret scans.
- Confirm `main`, `origin/main`, and the intended tag target are the same commit.

After publication:

- Confirm `v0.1.0` resolves to the intended commit.
- Confirm the GitHub release is published, marked prerelease, and not marked latest.
- Confirm all twelve expected assets exist and no unexpected assets were uploaded.
- Download `SHA256SUMS` and every release asset into a fresh temporary directory and verify every checksum.
- Confirm the README's direct installer URLs return the published scripts.
- Record the successful release-workflow URL in the handoff.
