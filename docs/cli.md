# CLI reference

Install development dependencies with `python -m pip install -e '.[dev]'`. The
`csaf` executable uses `csaf.db` unless `--database PATH` or `CSAF_DATABASE` is
provided before the subcommand.

## Commands

```text
csaf skills list
csaf skill run NAME (--input JSON | --input-file FILE) [--output-dir DIR] [--include-artifact-content]
csaf account-brief CUSTOMER [--days N] [--output FILE]
csaf meeting analyze TRANSCRIPT --customer-id ID --meeting-id ID
csaf qbr generate CUSTOMER --quarter YYYY-QN [Office options]
csaf connector ingest {markdown|json|csv} SOURCE --customer-id ID
csaf memory inspect CUSTOMER [filters]
csaf evaluate DATASET [--report FILE]
```

For `skill run`, provide exactly one JSON source: `--input` for inline JSON or
`--input-file` for a UTF-8 file. A file avoids shell-quoting surprises and is the
recommended approach in Windows PowerShell:

```powershell
$inputFile = Join-Path $PWD "account-brief-input.json"
[IO.File]::WriteAllText($inputFile, '{"customer_id":"acme"}')
csaf skill run account-brief --input-file $inputFile --output-dir .\artifacts
```

For advanced one-off inline input, PowerShell's stop-parsing token avoids its
normal argument rewriting; escape the JSON quotes for native Windows argument
parsing:

```powershell
csaf --% skill run account-brief --input "{\"customer_id\":\"acme\"}"
```

`--output-dir` atomically writes every generated artifact into the directory
before Customer Memory changes are committed. JSON output includes artifact
metadata but omits artifact `content` by default; add
`--include-artifact-content` when the base64-encoded content is needed inline.

Commands emit JSON to standard output. Human-readable errors go to standard error.
Normal success exits `0`; validation/configuration errors exit `2`; `evaluate`
uses exit `1` when valid golden cases detect a regression.

Use `csaf COMMAND --help` for complete flags. QBR requires an OfficeCLI-compatible
renderer; see [OfficeCLI integration](officecli.md).
