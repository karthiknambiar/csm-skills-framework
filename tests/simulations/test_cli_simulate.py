"""CLI coverage for deterministic simulation replay reports."""

import json
from pathlib import Path

import pytest
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


def test_simulate_replay_command_is_relative_and_shell_safe(tmp_path: Path, monkeypatch) -> None:
    dataset = tmp_path / "data set;still-safe"
    _write_dataset(dataset, _scenario("first"))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["simulate", str(dataset), "--report-dir", "reports"])

    assert result.exit_code == 0
    command = json.loads((tmp_path / "reports" / "simulation-report.json").read_text())[
        "scenarios"
    ][0]["replay_argv"]
    assert command[0:2] == ["csaf", "simulate"]
    assert "<redacted" not in command


def test_simulate_execution_failure_writes_reports_and_exits_one(tmp_path: Path) -> None:
    dataset = tmp_path / "execution-failure"
    scenario = _scenario("execution-failure")
    scenario["steps"][0]["skill"] = "missing-skill"
    _write_dataset(dataset, scenario)
    report_dir = tmp_path / "execution-report"

    result = runner.invoke(app, ["simulate", str(dataset), "--report-dir", str(report_dir)])

    assert result.exit_code == 1
    assert (report_dir / "simulation-report.json").exists()


def test_simulate_uses_default_and_explicit_fixture_roots(tmp_path: Path) -> None:
    dataset = tmp_path / "scenarios"
    scenario = _scenario("fixture")
    scenario["steps"] = [
        {"type": "ingest_fixture", "customer_id": "acme", "fixture": "records.json"}
    ]
    scenario["expectations"] = [{"type": "memory_count", "customer_id": "acme", "count": 1}]
    _write_dataset(dataset, scenario)
    default_root = dataset / "fixtures"
    default_root.mkdir()
    (default_root / "records.json").write_text(
        '{"records":[{"id":"one","kind":"risk","content":"risk"}]}', encoding="utf-8"
    )
    default = runner.invoke(
        app, ["simulate", str(dataset), "--report-dir", str(tmp_path / "default")]
    )
    assert default.exit_code == 0
    default_argv = json.loads((tmp_path / "default" / "simulation-report.json").read_text())[
        "scenarios"
    ][0]["replay_argv"]
    assert "--fixture-root" not in default_argv
    explicit_root = tmp_path / "custom-fixtures"
    explicit_root.mkdir()
    (explicit_root / "records.json").write_text(
        '{"records":[{"id":"one","kind":"risk","content":"risk"}]}', encoding="utf-8"
    )
    explicit = runner.invoke(
        app,
        [
            "simulate",
            str(dataset),
            "--fixture-root",
            str(explicit_root),
            "--report-dir",
            str(tmp_path / "explicit"),
        ],
    )
    assert explicit.exit_code == 0
    explicit_argv = json.loads((tmp_path / "explicit" / "simulation-report.json").read_text())[
        "scenarios"
    ][0]["replay_argv"]
    assert explicit_argv[-2:] == ["--fixture-root", str(explicit_root.resolve())]


def test_replay_argv_preserves_metacharacters_without_relpath(tmp_path: Path) -> None:
    dataset = tmp_path / "a&b$c()``"
    _write_dataset(dataset, _scenario("first"))
    result = runner.invoke(
        app, ["simulate", str(dataset), "--report-dir", str(tmp_path / "reports")]
    )
    assert result.exit_code == 0
    argv = json.loads((tmp_path / "reports" / "simulation-report.json").read_text())["scenarios"][
        0
    ]["replay_argv"]
    assert argv[2] == str(dataset.resolve())


def test_simulate_rejects_sensitive_replay_locators_without_emitting_them(tmp_path: Path) -> None:
    unsafe_dataset = tmp_path / "owner@example.test"
    _write_dataset(unsafe_dataset, _scenario("first"))
    fixture_root = tmp_path / "api_key=topsecret"
    fixture_root.mkdir()
    report_dir = tmp_path / "reports"

    result = runner.invoke(
        app,
        [
            "simulate",
            str(unsafe_dataset),
            "--fixture-root",
            str(fixture_root),
            "--report-dir",
            str(report_dir),
        ],
    )

    assert result.exit_code == 2
    assert result.output == "Error: simulation replay configuration is unsafe\n"
    assert "owner@example.test" not in result.output
    assert "topsecret" not in result.output
    assert not report_dir.exists()


@pytest.mark.parametrize(
    "unsafe_locator",
    ["Bearer abcdef.ghijkl.mnopqr", "aaaa.bbbb.cccc"],
)
def test_simulate_rejects_bearer_and_jwt_fixture_locators(
    tmp_path: Path, unsafe_locator: str
) -> None:
    dataset = tmp_path / "scenarios"
    _write_dataset(dataset, _scenario("first"))
    fixture_root = tmp_path / unsafe_locator
    fixture_root.mkdir()

    result = runner.invoke(app, ["simulate", str(dataset), "--fixture-root", str(fixture_root)])

    assert result.exit_code == 2
    assert result.output == "Error: simulation replay configuration is unsafe\n"
    assert unsafe_locator not in result.output
