# Tutorial: use CSAF from your assistant

This walkthrough starts with a native Codex, Claude Code, or Gemini CLI installation. CSAF processes customer context locally. It does not require SaaS credentials, an API key, or a hosted AI service.

## 1. Check the installation

Ask your assistant:

> "Check whether CSAF is ready on this machine. Explain any failed check, but do not install or repair anything without asking me first."

The assistant runs the native diagnostic:

```bash
csaf setup doctor
```

For structured diagnostic output:

```bash
csaf setup doctor --json
```

Exit code `0` means the runtime, required OfficeCLI binary, detected adapters, and permissions are ready. Exit code `2` needs attention. Follow the [installation troubleshooting guide](installation.md#troubleshooting) and approve a repair only after reviewing its plan.

## 2. Add customer context

The repository includes normalized sample records. Ask:

> "Load `examples/data/acme-memory.json` into Customer Memory for the exact customer ID `acme`. Tell me which local database will change before you run it."

The equivalent local command is:

```bash
csaf --database tutorial.db connector ingest json examples/data/acme-memory.json --customer-id acme
```

Inspect current risks without changing memory:

```bash
csaf --database tutorial.db memory inspect acme --kind risk --latest-only
```

## 3. Create an Account Brief

Ask:

> "Prepare an Account Brief for customer `acme` from the last 90 days in Customer Memory. Save it as `acme-brief.md` and keep missing facts explicit."

The local workflow command is:

```bash
csaf --database tutorial.db account-brief acme --days 90 --output acme-brief.md
```

The brief cites the Customer Memory records it uses. A successful run may append derived brief context to Customer Memory, so the assistant explains that change and the output path before execution.

## 4. Analyze a customer meeting

Ask:

> "Use Meeting Copilot on `examples/data/acme-meeting.md` for customer `acme` and meeting ID `acme-kickoff`. Separate actions from commitments, preserve the transcript evidence, and save the analysis as `acme-meeting-analysis.md`."

The local workflow command is:

```bash
csaf --database tutorial.db meeting analyze examples/data/acme-meeting.md --customer-id acme --meeting-id acme-kickoff --output acme-meeting-analysis.md
```

Meeting Copilot writes grounded meeting, timeline, action, commitment, risk, and product-feedback records only after a successful analysis. Regenerate the Account Brief to include the retained meeting context.

## 5. Generate QBR documents

OfficeCLI is mandatory for QBR PowerPoint and Word output. Ask your assistant to check it locally:

```bash
csaf office doctor --json
```

You can provide your own QBR template for PowerPoint, Word, or both. If you request a template but have not supplied an accessible file, the assistant asks for it instead of substituting another file. For a format without a user template, CSAF uses its bundled, vetted generic QBR template. It never searches for or downloads a template at runtime.

With a PowerPoint template and the bundled Word template, ask:

> "Create the 2026-Q3 QBR for customer `acme` in `generated/`. Use my PowerPoint template at `brand-qbr.pptx`; use CSAF's bundled vetted template for Word. Tell me about Customer Memory changes before running it."

The local workflow command is:

```bash
csaf --database tutorial.db qbr generate acme --quarter 2026-Q3 --powerpoint-template brand-qbr.pptx --output-dir generated
```

Only treat the QBR as delivered after the command exits successfully and both reported artifact paths exist. CSAF preserves the source template.

## 6. Check for updates

Ask:

> "Check whether a stable CSAF update is available. Notify me only; do not install it."

The assistant runs:

```bash
csaf setup check-update --json
```

The native launcher also performs this cached check at most once per 24 hours. It never auto-installs updates. Applying an update requires a separate `csaf setup update` command and fresh explicit consent.

## Continue

Read the [CLI reference](cli.md) for structured automation, the [OfficeCLI guide](officecli.md) for document diagnostics, or the [Customer Memory model](memory-model.md) for revision and provenance behavior.
