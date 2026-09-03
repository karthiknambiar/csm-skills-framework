"""Typer command-line interface for CSAF."""

import json
import tempfile
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from csaf.cli.artifacts import deliver_artifacts
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
from csaf.office import OfficeCLIDoctor, OfficeCLIError
from csaf.schemas import MemoryKind, MemoryQuery
from csaf.setup.cli import setup_app
from csaf.simulations import (
    DeterministicGrader,
    JourneyRunner,
    SimulationDatasetError,
    SimulationWorld,
    load_scenarios,
)
from csaf.simulations.reporting import (
    SIMULATION_EPOCH,
    SimulationScenarioReport,
    SimulationSuiteReport,
    validate_replay_argv_safety,
    write_report_files,
)
from csaf.skills import Artifact
from csaf.skills.builtin import QBRSkill
from csaf.skills.errors import SkillError, SkillExecutionError

app = typer.Typer(name="csaf", help="Customer Success Agent Framework CLI.")
skills_app = typer.Typer(help="Discover available skills.")
skill_app = typer.Typer(help="Run a reusable skill.")
memory_app = typer.Typer(help="Inspect Customer Memory.")
meeting_app = typer.Typer(help="Analyze customer meetings.")
qbr_app = typer.Typer(help="Generate quarterly business reviews.")
office_app = typer.Typer(help="Check local OfficeCLI readiness.")
connector_app = typer.Typer(help="Ingest normalized customer data.")
app.add_typer(skills_app, name="skills")
app.add_typer(skill_app, name="skill")
app.add_typer(memory_app, name="memory")
app.add_typer(meeting_app, name="meeting")
app.add_typer(qbr_app, name="qbr")
app.add_typer(office_app, name="office")
app.add_typer(connector_app, name="connector")
app.add_typer(setup_app, name="setup")


def _runtime(context: typer.Context) -> Runtime:
    return context.ensure_object(dict)["runtime"]


def _emit(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


def _deliver_to_directory(artifacts: tuple[Artifact, ...], *, output_dir: Path) -> None:
    destinations = {artifact.filename: output_dir / artifact.filename for artifact in artifacts}
    deliver_artifacts(artifacts, destinations)


def _preflight_officecli(runtime: Runtime) -> None:
    skill = runtime.skills.get("qbr")
    if not isinstance(skill, QBRSkill):
        raise SkillExecutionError("registered qbr skill does not support Office preflight")
    skill.preflight()


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

    if context.invoked_subcommand in {"setup", "simulate"}:
        context.obj = {}
        return
    context.obj = {"runtime": create_runtime(database)}
    context.call_on_close(context.obj["runtime"].memory.close)


def _simulation_fixture_root(dataset: Path, explicit_root: Path | None) -> Path:
    """Choose a local fixture root without exposing filesystem details in errors."""

    root = (
        explicit_root
        if explicit_root is not None
        else (dataset.parent / "fixtures" if dataset.is_file() else dataset / "fixtures")
    )
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("simulation fixture root is unsafe")
    return root.resolve()


def _replay_argv(
    dataset: Path, scenario_id: str, seed: int, fixture_root: Path | None
) -> tuple[str, ...]:
    """Build a structured replay vector; it is data, never shell text."""

    arguments = [
        "csaf",
        "simulate",
        str(dataset.resolve()),
        "--scenario",
        scenario_id,
        "--seed",
        str(seed),
    ]
    if fixture_root is not None:
        arguments.extend(["--fixture-root", str(fixture_root.resolve())])
    replay_argv = tuple(arguments)
    validate_replay_argv_safety(replay_argv)
    return replay_argv


@office_app.command("doctor")
def office_doctor(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the deterministic diagnostic report as JSON."),
    ] = False,
) -> None:
    """Check whether local OfficeCLI can safely render QBR artifacts."""

    try:
        report = OfficeCLIDoctor().run()
    except (OSError, OfficeCLIError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error

    if json_output:
        _emit(report.model_dump(mode="json"))
    else:
        for check in report.checks:
            typer.echo(f"[{check.status.value.upper()}] {check.name}: {check.message}")
        if not report.ready:
            typer.echo("\nOfficeCLI installation and update guidance:")
            typer.echo(
                "  Windows PowerShell: irm https://raw.githubusercontent.com/"
                "iOfficeAI/OfficeCLI/main/install.ps1 | iex"
            )
            typer.echo(
                "  macOS/Linux: curl -fsSL https://raw.githubusercontent.com/"
                "iOfficeAI/OfficeCLI/main/install.sh | sh"
            )
            typer.echo("  Project: https://github.com/iOfficeAI/OfficeCLI")
            typer.echo("CSAF reports these instructions only; it never installs software.")
    if not report.ready:
        raise typer.Exit(code=2)


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


@app.command("simulate")
def simulate(
    dataset: Annotated[Path, typer.Argument(help="Simulation JSON file or directory.")],
    scenario: Annotated[
        list[str] | None,
        typer.Option("--scenario", help="Run exactly one scenario ID."),
    ] = None,
    seed: Annotated[
        int | None,
        typer.Option("--seed", help="Override the seed for one selected scenario."),
    ] = None,
    report_dir: Annotated[
        Path,
        typer.Option("--report-dir", help="Directory for deterministic simulation reports."),
    ] = Path("simulation-results"),
    fixture_root: Annotated[
        Path | None,
        typer.Option("--fixture-root", help="Root directory for local connector fixtures."),
    ] = None,
) -> None:
    """Run isolated deterministic customer journeys and write replayable reports."""

    try:
        loaded = load_scenarios(dataset)
        fixtures = _simulation_fixture_root(dataset, fixture_root)
        requested = tuple(scenario or ())
        if len(set(requested)) != len(requested):
            raise ValueError("duplicate scenario selection")
        scenarios_by_id = {item.id: item for item in loaded}
        unknown = next((item for item in requested if item not in scenarios_by_id), None)
        if unknown is not None:
            raise ValueError("unknown scenario id")
        selected = tuple(scenarios_by_id[item] for item in requested) if requested else loaded
        if seed is not None and len(selected) != 1:
            raise ValueError("--seed requires exactly one selected scenario")
        prepared = tuple(
            (
                configured.model_copy(update={"seed": seed}) if seed is not None else configured,
                _replay_argv(
                    dataset,
                    configured.id,
                    seed if seed is not None else configured.seed,
                    fixture_root,
                ),
            )
            for configured in selected
        )
    except (OSError, SimulationDatasetError, ValidationError, ValueError) as error:
        message = (
            "simulation replay configuration is unsafe"
            if isinstance(error, ValueError)
            and str(error) == "simulation replay configuration is unsafe"
            else str(error)
            if isinstance(error, ValueError) and not isinstance(error, SimulationDatasetError)
            else "unable to load simulation dataset"
        )
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=2) from error

    try:
        results: list[SimulationScenarioReport] = []
        with tempfile.TemporaryDirectory(prefix="csaf-simulate-") as temporary_root:
            run_root = Path(temporary_root)
            for index, (effective, replay_argv) in enumerate(prepared, start=1):
                with SimulationWorld.create(
                    run_root / f"scenario-{index}", SIMULATION_EPOCH, effective.seed
                ) as world:
                    run = JourneyRunner(world, fixture_root=fixtures).run(effective)
                grade = DeterministicGrader().grade(effective, run)
                results.append(
                    SimulationScenarioReport(
                        scenario_id=effective.id,
                        scenario_title=effective.title,
                        seed=effective.seed,
                        run=run,
                        grade=grade,
                        passed=run.success and grade.passed,
                        replay_argv=replay_argv,
                    )
                )
        passed_count = sum(result.passed for result in results)
        report = SimulationSuiteReport(
            schema_version=1,
            started_at=SIMULATION_EPOCH,
            config={
                "epoch": "2026-01-01T00:00:00Z",
                "fixture_root": "explicit" if fixture_root is not None else "dataset-default",
                "seed_override": seed,
            },
            total=len(results),
            passed_count=passed_count,
            failed_count=len(results) - passed_count,
            passed=all(result.passed for result in results),
            scenarios=tuple(results),
        )
        paths = write_report_files(report, report_dir)
    except Exception as error:
        typer.echo("Error: simulation infrastructure failure", err=True)
        raise typer.Exit(code=2) from error

    typer.echo(
        f"{report.passed_count}/{report.total} passed; {report.failed_count} failed. "
        f"Report: {paths[0].parent}"
    )
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
        artifact_filename = f"{customer_id}-account-brief.md"
        destinations = {artifact_filename: output} if output is not None else {}
        result = _runtime(context).runner.run(
            "account-brief",
            {"customer_id": customer_id, "time_window_days": days},
            artifact_handler=partial(deliver_artifacts, destinations=destinations),
        )
    except (OSError, ValidationError, SkillError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
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
        artifact_filename = f"{meeting_id}-meeting-analysis.md"
        destinations = {artifact_filename: output} if output is not None else {}
        result = _runtime(context).runner.run(
            "meeting-copilot",
            {
                "customer_id": customer_id,
                "meeting_id": meeting_id,
                "transcript": transcript_text,
                "attendees": attendee or (),
            },
            artifact_handler=partial(deliver_artifacts, destinations=destinations),
        )
    except (OSError, UnicodeError, ValidationError, SkillError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
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
        runtime = _runtime(context)
        _preflight_officecli(runtime)
        result = runtime.runner.run(
            "qbr",
            {
                "customer_id": customer_id,
                "quarter": quarter,
                "powerpoint_template": powerpoint_template,
                "word_template": word_template,
                "existing_powerpoint": existing_powerpoint,
                "existing_word": existing_word,
            },
            artifact_handler=partial(_deliver_to_directory, output_dir=output_dir),
        )
    except (OSError, OfficeCLIError, ValidationError, SkillError) as error:
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
        str | None,
        typer.Option("--input", help="Skill input as a JSON object."),
    ] = None,
    input_file: Annotated[
        Path | None,
        typer.Option("--input-file", help="Read skill input from a UTF-8 JSON file."),
    ] = None,
    include_artifact_content: Annotated[
        bool,
        typer.Option(
            "--include-artifact-content",
            help="Include base64-encoded artifact content in JSON output.",
        ),
    ] = False,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Write all artifacts to this directory."),
    ] = None,
) -> None:
    """Validate and run a skill with JSON input."""

    try:
        if (input_json is None) == (input_file is None):
            raise ValueError("provide exactly one of --input or --input-file")
        raw_input = input_file.read_text(encoding="utf-8") if input_file is not None else input_json
        payload = json.loads(raw_input)
        if not isinstance(payload, dict):
            raise ValueError("skill input must be a JSON object")
        artifact_handler = (
            partial(_deliver_to_directory, output_dir=output_dir)
            if output_dir is not None
            else None
        )
        runtime = _runtime(context)
        if name == "qbr":
            _preflight_officecli(runtime)
        result = runtime.runner.run(name, payload, artifact_handler=artifact_handler)
    except (
        json.JSONDecodeError,
        OSError,
        OfficeCLIError,
        UnicodeError,
        ValueError,
        ValidationError,
        SkillError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    exclude = None
    if not include_artifact_content:
        exclude = {"artifacts": {"__all__": {"content"}}}
    _emit(result.model_dump(mode="json", exclude=exclude))


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
