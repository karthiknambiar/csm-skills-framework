# Runnable examples

Install CSAF in a Python 3.11+ environment, then run these scripts from the
repository root:

```bash
python examples/account_brief.py
python examples/meeting_copilot.py
python examples/ingest_json.py
```

The examples use in-memory SQLite and bundled local files. They do not contact an
LLM provider or SaaS service and do not leave a database behind.

- `account_brief.py` seeds grounded account facts and prints the Markdown brief.
- `meeting_copilot.py` analyzes the sample transcript and prints structured JSON.
- `ingest_json.py` imports the canonical JSON fixture and prints retained memory.

See the full [tutorial](../docs/tutorial.md) for the equivalent CLI workflow.

## PowerShell-friendly skill input

For generic skill execution, put the input in a UTF-8 JSON file instead of
escaping JSON at the PowerShell prompt:

```powershell
csaf --database tutorial.db skill run account-brief `
  --input-file examples/data/account-brief-input.json
```

Artifact bytes are omitted from the JSON response by default. Add
`--include-artifact-content` only when a downstream tool needs them, or use
`--output-dir generated` to write every artifact safely to a directory.

QBR output requires the local deterministic OfficeCLI. Check it before running
QBR:

```powershell
csaf office doctor
csaf office doctor --json
```

The doctor reports installation and compatibility guidance; it never installs
software, contacts a hosted AI service, or asks for an API key.
