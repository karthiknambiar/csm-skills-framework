"""CLI transport tests using a persistent temporary SQLite database."""

# ruff: noqa: E402 -- optional transport dependency is checked before imports

import json
from pathlib import Path

import pytest

typer = pytest.importorskip("typer")
from typer.testing import CliRunner

from csaf.cli import app
from csaf.memory import SQLiteMemoryStore
from csaf.schemas import MemoryKind, MemoryQuery, MemoryRecordCreate

runner = CliRunner()


def test_skills_list_returns_json(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"

    result = runner.invoke(app, ["--database", str(database), "skills", "list"])

    assert result.exit_code == 0
    assert [skill["name"] for skill in json.loads(result.stdout)] == [
        "account-brief",
        "meeting-copilot",
        "qbr",
    ]


def test_account_brief_writes_markdown_artifact(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"
    output = tmp_path / "brief.md"

    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "account-brief",
            "acme",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["output"]["customer_id"] == "acme"
    assert output.read_text().startswith("# Account Brief: acme")


def test_meeting_analyze_reads_transcript_and_writes_artifact(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.md"
    transcript.write_text("Alex: Action: send the rollout plan.")
    output = tmp_path / "analysis.md"

    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "meeting",
            "analyze",
            str(transcript),
            "--customer-id",
            "acme",
            "--meeting-id",
            "meeting-1",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["output"]["action_items"][0]["speaker"] == "Alex"
    assert output.read_text().startswith("# Meeting Analysis: meeting-1")


def test_memory_inspect_reads_configured_database(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"
    with SQLiteMemoryStore(database) as memory:
        memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.COMMITMENT,
                content="Send the migration plan.",
            )
        )

    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "memory",
            "inspect",
            "acme",
            "--kind",
            "commitment",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["content"] == "Send the migration plan."


def test_skill_run_returns_nonzero_for_unknown_skill(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "skill",
            "run",
            "unknown",
            "--input",
            '{"customer_id":"acme"}',
        ],
    )

    assert result.exit_code == 2
    assert "skill is not registered: unknown" in result.stderr


def test_connector_ingest_writes_memory_and_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "records.json"
    source.write_text('[{"id":"risk-1","kind":"risk","content":"Renewal risk."}]')
    database = tmp_path / "memory.db"
    checkpoint = tmp_path / "checkpoint.json"

    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "connector",
            "ingest",
            "json",
            str(source),
            "--customer-id",
            "acme",
            "--checkpoint-file",
            str(checkpoint),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["records_written"] == 1
    assert json.loads(checkpoint.read_text())["state"]["completed"] is True
    with SQLiteMemoryStore(database) as memory:
        assert memory.search(MemoryQuery(customer_id="acme", kinds=(MemoryKind.RISK,)))


def test_evaluate_writes_passing_regression_report(tmp_path: Path) -> None:
    report = tmp_path / "evaluation-report.json"

    result = runner.invoke(
        app,
        ["evaluate", "evaluations/golden", "--report", str(report)],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["pass_rate"] == 1.0
    assert json.loads(report.read_text())["passed"] is True
