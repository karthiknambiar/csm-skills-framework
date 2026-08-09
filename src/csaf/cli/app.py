"""Typer command-line interface for CSAF."""

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from csaf.connectors import (
    ConnectorCheckpoint,
    ConnectorIngestor,
    CSVConnector,
    JSONConnector,
    MarkdownConnector,
)
from csaf.connectors.errors import ConnectorError
from csaf.core import Runtime, create_runtime
from csaf.evaluations import EvaluationRunner, load_golden_cases
from csaf.evaluations.loader import GoldenDatasetError
from csaf.schemas import MemoryKind, MemoryQuery
from csaf.skills.errors import SkillError

app = typer.Typer(name="csaf", help="Customer Success Agent Framework CLI.")
skills_app = typer.Typer(help="Discover available skills.")
skill_app = typer.Typer(help="Run a reusable skill.")
memory_app = typer.Typer(help="Inspect Customer Memory.")
meeting_app = typer.Typer(help="Analyze customer meetings.")
qbr_app = typer.Typer(help="Generate quarterly business reviews.")
connector_app = typer.Typer(help="Ingest normalized customer data.")
app.add_typer(skills_app, name="skills")
app.add_typer(skill_app, name="skill")
app.add_typer(memory_app, name="memory")
app.add_typer(meeting_app, name="meeting")
app.add_typer(qbr_app, name="qbr")
app.add_typer(connector_app, name="connector")


def _runtime(context: typer.Context) -> Runtime:
    return context.ensure_object(dict)["runtime"]


def _emit(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


@app.callback()
def initialize(
    context: typer.Context,
    database: Annotated[
        Path,
        typer.Option(
            "--database",
            envvar="CSAF_DATABASE",
            help="SQLite Customer Memory database path.",
        ),
    ] = Path("csaf.db"),
) -> None:
    """Configure a local CSAF runtime for this invocation."""

    context.obj = {"runtime": create_runtime(database)}
    context.call_on_close(context.obj["runtime"].memory.close)


@app.command("evaluate")
def evaluate(
    dataset: Annotated[Path, typer.Argument(help="Golden JSON file or directory.")],
    report_file: Annotated[
        Path | None,
        typer.Option("--report", help="Write the complete regression report as JSON."),
    ] = None,
) -> None:
    """Run deterministic skill regressions and exit nonzero on failure."""

    try:
        report = EvaluationRunner().run(load_golden_cases(dataset))
        if report_file is not None:
            report_file.parent.mkdir(parents=True, exist_ok=True)
            report_file.write_text(report.model_dump_json(indent=2))
    except (OSError, GoldenDatasetError, ValidationError, SkillError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    summary = {
        "passed": report.passed,
        "cases_total": report.cases_total,
        "cases_passed": report.cases_passed,
        "pass_rate": report.pass_rate,
    }
    _emit(summary)
    if not report.passed:
        raise typer.Exit(code=1)


@app.command("account-brief")
def account_brief(
    context: typer.Context,
    customer_id: Annotated[str, typer.Argument(help="Customer identifier.")],
    days: Annotated[
        int | None,
        typer.Option("--days", min=1, max=3_650, help="Optional lookback window."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write the Markdown artifact to this path."),
    ] = None,
) -> None:
    """Generate a grounded Account Brief for a customer."""

    try:
        result = _runtime(context).runner.run(
            "account-brief",
            {"customer_id": customer_id, "time_window_days": days},
        )
    except (ValidationError, SkillError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    if output is not None:
        output.write_bytes(result.artifacts[0].content)
    _emit(result.model_dump(mode="json"))


@meeting_app.command("analyze")
def analyze_meeting(
    context: typer.Context,
    transcript: Annotated[Path, typer.Argument(help="UTF-8 transcript file.")],
    customer_id: Annotated[str, typer.Option("--customer-id", help="Customer identifier.")],
    meeting_id: Annotated[str, typer.Option("--meeting-id", help="Stable meeting identifier.")],
    attendee: Annotated[
        list[str] | None,
        typer.Option("--attendee", help="Repeat for each meeting attendee."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write the Markdown analysis to this path."),
    ] = None,
) -> None:
    """Analyze a transcript, update memory, and generate follow-up material."""

    try:
        transcript_text = transcript.read_text(encoding="utf-8")
        result = _runtime(context).runner.run(
            "meeting-copilot",
            {
                "customer_id": customer_id,
                "meeting_id": meeting_id,
                "transcript": transcript_text,
                "attendees": attendee or (),
            },
        )
    except (OSError, UnicodeError, ValidationError, SkillError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    if output is not None:
        output.write_bytes(result.artifacts[0].content)
    _emit(result.model_dump(mode="json"))


@qbr_app.command("generate")
def generate_qbr(
    context: typer.Context,
    customer_id: Annotated[str, typer.Argument(help="Customer identifier.")],
    quarter: Annotated[str, typer.Option("--quarter", help="Quarter in YYYY-QN format.")],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Directory for PowerPoint and Word files."),
    ] = Path("."),
    powerpoint_template: Annotated[Path | None, typer.Option("--powerpoint-template")] = None,
    word_template: Annotated[Path | None, typer.Option("--word-template")] = None,
    existing_powerpoint: Annotated[Path | None, typer.Option("--existing-powerpoint")] = None,
    existing_word: Annotated[Path | None, typer.Option("--existing-word")] = None,
) -> None:
    """Create or update a cited QBR through the configured OfficeCLI adapter."""

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        result = _runtime(context).runner.run(
            "qbr",
            {
                "customer_id": customer_id,
                "quarter": quarter,
                "powerpoint_template": powerpoint_template,
                "word_template": word_template,
                "existing_powerpoint": existing_powerpoint,
                "existing_word": existing_word,
            },
        )
        for artifact in result.artifacts:
            (output_dir / artifact.filename).write_bytes(artifact.content)
    except (OSError, ValidationError, SkillError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    _emit(result.model_dump(mode="json", exclude={"artifacts": {"__all__": {"content"}}}))


@connector_app.command("ingest")
def ingest_connector(
    context: typer.Context,
    format: Annotated[
        str,
        typer.Argument(help="Local source format: markdown, json, or csv."),
    ],
    source: Annotated[Path, typer.Argument(help="Source file or directory.")],
    customer_id: Annotated[str, typer.Option("--customer-id", help="Customer identifier.")],
    default_kind: Annotated[
        MemoryKind,
        typer.Option("--default-kind", help="Kind used when a source omits one."),
    ] = MemoryKind.TIMELINE,
    checkpoint_file: Annotated[
        Path | None,
        typer.Option("--checkpoint-file", help="Read and update resumable state as JSON."),
    ] = None,
    page_size: Annotated[int, typer.Option(min=1, max=1_000)] = 100,
) -> None:
    """Ingest a local source through the connector normalization lifecycle."""

    connector_types = {
        "markdown": MarkdownConnector,
        "json": JSONConnector,
        "csv": CSVConnector,
    }
    try:
        connector_type = connector_types[format.casefold()]
        connector = connector_type(source, default_kind=default_kind)
        checkpoint = (
            ConnectorCheckpoint.model_validate_json(checkpoint_file.read_text())
            if checkpoint_file is not None and checkpoint_file.exists()
            else None
        )
        result = ConnectorIngestor(_runtime(context).memory).ingest(
            connector,
            customer_id,
            checkpoint=checkpoint,
            page_size=page_size,
        )
        if checkpoint_file is not None:
            checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
            checkpoint_file.write_text(result.checkpoint.model_dump_json(indent=2))
    except KeyError as error:
        typer.echo(f"Error: unsupported connector format: {format}", err=True)
        raise typer.Exit(code=2) from error
    except (OSError, ValidationError, ConnectorError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    _emit(result.model_dump(mode="json"))


@skills_app.command("list")
def list_skills(context: typer.Context) -> None:
    """List registered skill contracts."""

    _emit([skill.metadata.model_dump(mode="json") for skill in _runtime(context).skills])


@skill_app.command("run")
def run_skill(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Registered skill name.")],
    input_json: Annotated[
        str,
        typer.Option("--input", help="Skill input as a JSON object."),
    ],
) -> None:
    """Validate and run a skill with JSON input."""

    try:
        payload = json.loads(input_json)
        if not isinstance(payload, dict):
            raise ValueError("skill input must be a JSON object")
        result = _runtime(context).runner.run(name, payload)
    except (json.JSONDecodeError, ValueError, ValidationError, SkillError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    _emit(result.model_dump(mode="json"))


@memory_app.command("inspect")
def inspect_memory(
    context: typer.Context,
    customer_id: Annotated[str, typer.Argument(help="Customer identifier.")],
    kind: Annotated[
        list[MemoryKind] | None,
        typer.Option("--kind", help="Repeat to include multiple memory categories."),
    ] = None,
    text: Annotated[str | None, typer.Option(help="Case-insensitive text filter.")] = None,
    latest_only: Annotated[
        bool,
        typer.Option("--latest-only", help="Return only the latest logical revisions."),
    ] = False,
    limit: Annotated[int, typer.Option(min=1, max=1_000)] = 100,
) -> None:
    """Inspect customer-scoped memory records."""

    try:
        records = _runtime(context).memory.search(
            MemoryQuery(
                customer_id=customer_id,
                kinds=tuple(kind or ()),
                text=text,
                latest_only=latest_only,
                limit=limit,
            )
        )
    except ValidationError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    _emit([record.model_dump(mode="json") for record in records])


def main() -> None:
    """Run the installed console entry point."""

    app()
