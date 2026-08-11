# Tutorial: build customer context and run skills

This tutorial uses bundled sample data and deterministic local processing. The
Account Brief and Meeting Copilot steps do not require SaaS credentials or
OfficeCLI. QBR documents require a compatible local OfficeCLI installation, but
never an API key or hosted AI service.

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

## 2. Ingest normalized records

```bash
csaf --database tutorial.db connector ingest json examples/data/acme-memory.json \
  --customer-id acme
```

Inspect current risks:

```bash
csaf --database tutorial.db memory inspect acme --kind risk --latest-only
```

## 3. Generate an Account Brief

```bash
csaf --database tutorial.db account-brief acme --output acme-brief.md
```

Open `acme-brief.md`. Evidence bullets contain `memory:UUID` citations, and a new
artifact revision is now present in `tutorial.db`.

For the generic skill command, the recommended PowerShell route is the bundled
UTF-8 JSON input file:

```powershell
csaf --database tutorial.db skill run account-brief `
  --input-file examples/data/account-brief-input.json
```

Use `--output-dir generated` to deliver artifacts to disk. The JSON response
omits artifact bytes unless `--include-artifact-content` is explicitly supplied.

## 4. Analyze a meeting

```bash
csaf --database tutorial.db meeting analyze examples/data/acme-meeting.md \
  --customer-id acme --meeting-id acme-kickoff \
  --output acme-meeting-analysis.md
```

Meeting Copilot appends `meeting`, `timeline`, `action_item`, `commitment`,
`risk`, and `feature_request` records grounded in the transcript.
Actions capture customer next steps separately from promises or commitments.
Generate the Account Brief again to see the newly retained context.

## 5. Check OfficeCLI and generate QBR documents

QBR creates local PowerPoint and Word artifacts through OfficeCLI. Run the
preflight first:

```powershell
csaf office doctor
csaf office doctor --json
```

A failed check exits with code 2 and includes installation or upgrade guidance.
The doctor does not install anything. After it reports ready, generate both
artifacts locally:

```powershell
csaf --database tutorial.db qbr generate acme --quarter 2026-Q3 `
  --output-dir generated
```

## 6. Run regressions

```bash
csaf evaluate evaluations/golden --report evaluation-report.json
```

The process exits nonzero if a versioned expectation regresses. Review the report's
category scores and findings before changing a golden baseline.

## Python examples

For programmatic usage, run:

```bash
python examples/account_brief.py
python examples/meeting_copilot.py
python examples/ingest_json.py
```

Each script owns and closes its runtime resources and can be adapted into a test,
job, notebook, or application composition root.
