# CLI reference

Install development dependencies with `python -m pip install -e '.[dev]'`. The
`csaf` executable uses `csaf.db` unless `--database PATH` or `CSAF_DATABASE` is
provided before the subcommand.

## Commands

```text
csaf skills list
csaf skill run NAME --input JSON
csaf account-brief CUSTOMER [--days N] [--output FILE]
csaf meeting analyze TRANSCRIPT --customer-id ID --meeting-id ID
csaf qbr generate CUSTOMER --quarter YYYY-QN [Office options]
csaf connector ingest {markdown|json|csv} SOURCE --customer-id ID
csaf memory inspect CUSTOMER [filters]
csaf evaluate DATASET [--report FILE]
```

Commands emit JSON to standard output. Human-readable errors go to standard error.
Normal success exits `0`; validation/configuration errors exit `2`; `evaluate`
uses exit `1` when valid golden cases detect a regression.

Use `csaf COMMAND --help` for complete flags. QBR requires an OfficeCLI-compatible
renderer; see [OfficeCLI integration](officecli.md).
