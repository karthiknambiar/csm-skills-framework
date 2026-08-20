# Account Brief

Use this workflow for a grounded, customer-scoped brief.

## Required input

- Customer identifier: obtain the exact stored identifier; do not derive one from a display name.
- Optional lookback: a positive number of days.
- Optional output path: obtain approval before writing.

If the identifier or underlying Customer Memory is unavailable, ask for it. Do not invent account facts. Disclose that a successful run may write derived brief context to Customer Memory and obtain explicit approval for that Customer Memory change before execution.

## Launcher arguments

Pass this launcher argument array exactly; the launcher already invokes `csaf`, so never prefix the array with another `csaf` token:

`["account-brief", "<customer-id>"]`

Append `"--days", "<days>"` and `"--output", "<approved-path>"` only when supplied.

Summarize only cited or returned facts. Keep unknowns explicit. Only claim delivery after exit code 0 and verification of the exact local path.