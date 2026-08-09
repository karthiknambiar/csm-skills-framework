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
