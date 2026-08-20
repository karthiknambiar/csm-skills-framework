# Meeting Copilot

Use this workflow to analyze supplied customer meeting notes or a transcript.

## Required input

- Transcript: an existing UTF-8 local file containing the supplied notes.
- Customer identifier: obtain the exact value; do not infer it.
- Meeting identifier: obtain a stable value; do not invent it.
- Optional attendees and approved output path.

Explain that successful execution updates Customer Memory before running it. Do not silently alter customer records. Separate explicit actions from promises; preserve stated owners and dates, and label missing ones unknown.

## Launcher arguments

Pass this launcher argument array exactly; the launcher already invokes `csaf`, so never prefix the array with another `csaf` token:

`["meeting", "analyze", "<transcript>", "--customer-id", "<customer-id>", "--meeting-id", "<meeting-id>"]`

Append each `"--attendee", "<name>"` pair and `"--output", "<approved-path>"` only when supplied.

Only claim delivery after exit code 0 and verification of the exact local path. If execution fails, claim no memory or artifact success.