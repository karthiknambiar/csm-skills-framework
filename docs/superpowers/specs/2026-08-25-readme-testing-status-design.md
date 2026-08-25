# README testing-status notice design

## Goal

Make the project’s current testing status visible before readers reach installation instructions, without weakening the existing consent, privacy, or stable dependency-version language.

## README change

Place this blockquote immediately after the opening value statement and before the OfficeCLI notice:

> **Testing status:** CSAF’s native agent integration is still being tested. Use non-production data, review each setup plan before consenting, and report unexpected behavior.

Keep the rest of the native-agent-first README structure unchanged.

## Verification

Extend the documentation contract to require the exact notice before `## Install`, retain the 180-nonblank-line limit, and preserve all existing installation, command, link, consent, privacy, and QBR-template checks.

Run the focused documentation suite, the full warning-as-error suite, Ruff lint and formatting checks, the combined worktree/tracked/history secret scan, and Git whitespace/status checks before integration.

## Publishing boundary

Commit the notice and its regression test on `codex/native-agent-installation`. Do not tag a stable release. Select the GitHub integration route only after verification; any push must use the existing `origin` repository and must not force-push.
