# Tutorial: build customer context and run skills

This tutorial uses the bundled sample data and an in-memory Python runtime, so it
does not require SaaS credentials or OfficeCLI.

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

## 4. Analyze a meeting

```bash
csaf --database tutorial.db meeting analyze examples/data/acme-meeting.md \
  --customer-id acme --meeting-id acme-kickoff \
  --output acme-meeting-analysis.md
```

Meeting Copilot appends meeting, timeline, commitment, risk, and product-feedback
records when those categories are grounded in the transcript. Generate the Account
Brief again to see the newly retained context.

## 5. Run regressions

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
