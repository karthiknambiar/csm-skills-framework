# OfficeCLI integration

CSAF sends a format-neutral JSON document specification to an external OfficeCLI
process. Keeping the process behind `OfficeArtifactRenderer` lets applications
test QBR content without installing Microsoft Office tooling and adapt to
different OfficeCLI distributions without changing skills.

## Default command contract

For new files, the default adapter invokes:

```bash
officecli create --format FORMAT --input document.json --output artifact.EXT
```

For updates, it invokes:

```bash
officecli update --format FORMAT --input document.json \
  --existing existing.EXT --output artifact.EXT
```

When a template is supplied, it also appends `--template PATH`. `FORMAT` is
`powerpoint` or `word`; extensions are `.pptx` and `.docx` respectively. The
process must exit successfully and create the requested output file.

OfficeCLI is intentionally an external runtime prerequisite rather than a Python
dependency. Install the OfficeCLI distribution used by your organization and
ensure its executable is on `PATH`.

## Adapting another OfficeCLI distribution

If its flags differ, configure argument templates at application startup:

```python
from csaf.office import OfficeCLIArtifactRenderer, OfficeCLIConfig

renderer = OfficeCLIArtifactRenderer(
    OfficeCLIConfig(
        executable="my-office-cli",
        create_arguments=(
            "render",
            "--kind",
            "{format}",
            "--spec",
            "{spec}",
            "--out",
            "{output}",
        ),
    )
)
```

Supported placeholders are `{format}`, `{spec}`, `{output}`, `{template}`, and
`{existing}`. Applications inject the renderer with
`create_runtime(office_renderer=renderer)`.

## Safety and lifecycle

- Specifications and outputs are created in a private temporary directory.
- The adapter uses argument arrays rather than a shell, preventing shell command
  interpolation.
- Execution has a configurable timeout and captures provider diagnostics.
- An update always requires an explicit existing file path.
- Skills append new QBR and artifact memory revisions even when OfficeCLI updates
  an existing file; earlier generation history is retained.
- Binary Office artifacts are base64 encoded when a skill result crosses the JSON
  API boundary; Python callers and the CLI file writer continue to receive bytes.
