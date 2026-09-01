"""CLI coverage for deterministic simulation replay reports."""

import json
from pathlib import Path

from typer.testing import CliRunner

from csaf.cli.app import app

runner = CliRunner()


def _scenario(
    identifier: str, *, expectation: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": identifier,
        "title": f"{identifier} journey",
        "seed": 17,
        "customers": ["acme"],
        "steps": [
            {
                "type": "run_skill",
                "skill": "account-brief",
                "input": {"customer_id": "acme"},
            }
        ],
        "expectations": [expectation or {"type": "output_present", "path": "customer_id"}],
    }


def _write_dataset(directory: Path, *scenarios: dict[str, object]) -> None:
    directory.mkdir()
    for scenario in scenarios:
        (directory / f"{scenario['id']}.json").write_text(json.dumps(scenario), encoding="utf-8")


def test_simulate_all_writes_deterministic_reports_and_ignores_global_database(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "scenarios"
    _write_dataset(dataset, _scenario("first"), _scenario("second"))
    report_dir = tmp_path / "reports"
    global_database = tmp_path / "global.db"

    result = runner.invoke(
        app,
        [
            "--database",
            str(global_database),
            "simulate",
            str(dataset),
            "--report-dir",
            str(report_dir),
        ],
    )

    assert result.exit_code == 0
    assert "2/2 passed" in result.stdout
    assert not global_database.exists()
    contents = {path.name: path.read_bytes() for path in sorted(report_dir.iterdir())}
    assert set(contents) == {
        "simulation-report.json",
        "simulation-report.md",
        "simulation-junit.xml",
    }
    rerun = runner.invoke(app, ["simulate", str(dataset), "--report-dir", str(report_dir)])
    assert rerun.exit_code == 0
    assert contents == {path.name: path.read_bytes() for path in sorted(report_dir.iterdir())}


def test_simulate_filter_and_seed_override_require_exact_single_selection(tmp_path: Path) -> None:
    dataset = tmp_path / "scenarios"
    _write_dataset(dataset, _scenario("first"), _scenario("second"))

    selected = runner.invoke(
        app,
        [
            "simulate",
            str(dataset),
            "--scenario",
            "first",
            "--seed",
            "99",
            "--report-dir",
            str(tmp_path / "one"),
        ],
    )
    assert selected.exit_code == 0
    report = json.loads((tmp_path / "one" / "simulation-report.json").read_text())
    assert [item["scenario_id"] for item in report["scenarios"]] == ["first"]
    assert report["scenarios"][0]["seed"] == 99

    ambiguous = runner.invoke(app, ["simulate", str(dataset), "--seed", "99"])
    assert ambiguous.exit_code == 2
    unknown = runner.invoke(app, ["simulate", str(dataset), "--scenario", "missing"])
    assert unknown.exit_code == 2
    assert "Traceback" not in unknown.output


def test_simulate_grade_failure_exits_one_and_runtime_errors_are_sanitized(tmp_path: Path) -> None:
    failed_dataset = tmp_path / "failed"
    _write_dataset(
        failed_dataset,
        _scenario(
            "failed", expectation={"type": "output_equals", "path": "customer_id", "value": "other"}
        ),
    )
    failed = runner.invoke(
        app, ["simulate", str(failed_dataset), "--report-dir", str(tmp_path / "failed-report")]
    )
    assert failed.exit_code == 1

    unsafe_dataset = tmp_path / "unsafe"
    _write_dataset(
        unsafe_dataset,
        _scenario(
            "unsafe",
            expectation={"type": "output_present", "path": "customer_id"},
        ),
    )
    (unsafe_dataset / "unsafe.json").write_text("{not json}", encoding="utf-8")
    unsafe = runner.invoke(app, ["simulate", str(unsafe_dataset)])
    assert unsafe.exit_code == 2
    assert "Traceback" not in unsafe.output
