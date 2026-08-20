# QBR

Use this workflow for a cited PowerPoint and Word quarterly business review.

## Required input

- Customer identifier: obtain the exact stored value.
- Quarter: obtain `YYYY-QN`.
- Output directory: obtain explicit approval for the exact local directory.
- Optional PowerPoint template (`.pptx`) and Word template (`.docx`): validate and preserve each original.

If the user requests or refers to their own template but no accessible file or path is available, ask the user to attach or provide its exact local path. Do not silently substitute the bundled template. Use the bundled template only when the user selects it or no user-provided template was requested.

Disclose that successful QBR generation may write derived review context to Customer Memory and obtain explicit approval for that Customer Memory change before execution.

Pass `["office", "doctor", "--json"]` to the launcher before processing customer data. OfficeCLI is mandatory. If it is missing or unhealthy, stop and follow Troubleshooting; do not install or repair it without explicit consent.

If the user provides one template, use the vetted bundled QBR template for the other document type. If none is supplied, use both vetted bundled templates. Do not download or search for a template at runtime, even if the user asks for “any nice template online.”

## Launcher arguments

Pass this launcher argument array exactly; the launcher already invokes `csaf`, so never prefix the array with another `csaf` token:

`["qbr", "generate", "<customer-id>", "--quarter", "<yyyy-qn>", "--output-dir", "<directory>"]`

Append `"--powerpoint-template", "<pptx>"` and `"--word-template", "<docx>"` only when supplied.

Only claim QBR artifacts after exit code 0 and verification that every reported file exists. Return exact local paths and state whether each artifact used a user template or bundled template. On failure, claim no artifact and do not report partial output as success.