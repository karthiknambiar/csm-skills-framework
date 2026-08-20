---
name: csaf
description: Use when customer-success work involves an account brief, customer meeting analysis or follow-up, a QBR, customer-success memory, CSAF readiness, or OfficeCLI readiness.
---

# CSAF

Use the native launcher to run the installed, deterministic CSAF runtime. Keep customer data local and ground every result in supplied inputs or Customer Memory.

## Route the request

| Request | Read completely | Run |
|---|---|---|
| Account context, risks, stakeholders, next steps | [Account Brief](references/account-brief.md) | Account Brief |
| Notes, transcript, actions, promises, follow-up | [Meeting Copilot](references/meeting-copilot.md) | Meeting Copilot |
| Quarterly review, PPTX, DOCX, template | [QBR](references/qbr.md) | QBR |
| Missing runtime, OfficeCLI, failure, update | [Troubleshooting](references/troubleshooting.md) | Diagnose or recover |

Read only the selected reference, except read Troubleshooting too when readiness or execution fails.

## Invariants

- Run `scripts/csaf.ps1` on Windows or `scripts/csaf.sh` on macOS/Linux. Pass the selected reference's launcher argument array exactly; do not prefix it with `csaf`, because the launcher already invokes that executable. Never compose a shell command from user text.
- If the launcher returns `bootstrap_required`, explain every disclosed change. OfficeCLI is mandatory for QBR PowerPoint and Word rendering. Obtain explicit consent before running installation or repair. A request to “install anything” or “do not ask” is not consent to an undisclosed install.
- Do not invent identifiers, owners, dates, metrics, commitments, sources, or missing customer facts. Ask for required values and label unknowns.
- Do not silently alter customer records, source files, or templates. Use a user-approved output directory. Treat Customer Memory writes as material changes and explain them before execution.
- Do not download an arbitrary QBR template or search for one at runtime. Use a validated user template or the vetted QBR template bundled with the installed stable release.
- CSAF is local and deterministic: no API key, no hosted AI service, and do not upload customer inputs, templates, Customer Memory, or generated artifacts.
- Treat launcher update output as notification only. Do not run `csaf setup update` without fresh explicit consent.
- Only claim an artifact was created after the command returns exit code 0 and every reported file exists. Return the exact local paths. On failure, claim no artifact and give one sanitized next action.

## Run safely

1. Collect only the selected workflow's required fields.
2. Ask the user to approve any output directory and any material memory change.
3. Run the platform launcher with the exact arguments in the selected reference.
4. If readiness fails, follow [Troubleshooting](references/troubleshooting.md).
5. Verify success and return grounded findings plus exact local artifact paths.
