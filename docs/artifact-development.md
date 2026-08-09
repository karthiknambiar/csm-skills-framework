# Artifact development guide

Artifacts are generated independently from a skill's structured output. This
allows Python, CLI, and REST callers to use validated data without opening a Word
document or presentation.

## Artifact contract

An `Artifact` declares:

- `type`: Markdown, Word, PowerPoint, Excel, PDF, or HTML.
- `filename`: suggested output filename.
- `media_type`: standard MIME type.
- `content`: artifact bytes.

Skills must declare every possible format in `SkillMetadata.artifacts`. The runner
rejects undeclared formats before memory effects are committed.

## Rendering separation

Skill code should first build structured output, then translate it into a
format-neutral rendering request. QBR uses `OfficeRenderRequest` sections with
bullets and memory citations; `OfficeArtifactRenderer` owns conversion to actual
Office bytes.

Use dependency injection for renderers:

```python
runtime = create_runtime(office_renderer=my_renderer)
```

This keeps tests deterministic and makes OfficeCLI distributions replaceable.
See [OfficeCLI integration](officecli.md) for subprocess configuration.

## Create, update, template, and version

- **Create** has no existing artifact path.
- **Update** requires an explicit existing path and should preserve relevant
  template/layout content.
- **Template-based generation** supplies a template separately from an update
  source.
- **Versioning** is stored in Customer Memory even when the underlying file is
  updated in place.

Binary artifact content is base64 encoded in JSON responses. Python callers and
CLI file writers receive the original bytes. Never place generated binary files
directly in Customer Memory; append artifact metadata and retain the file in an
appropriate object/file store.

## Testing

Renderer contract tests should verify argument construction, timeouts, provider
errors, missing files, and returned bytes. Skill tests should inject a recording
renderer and assert document sections, citations, operations, filenames, memory
revisions, and declared artifact types.
