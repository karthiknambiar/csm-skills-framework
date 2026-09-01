"""Deterministic, redacted simulation report coverage."""

from pathlib import Path

import pytest

from csaf.simulations import (
    DeterministicGrader,
    JourneyRunner,
    SimulationScenario,
    SimulationWorld,
)
from csaf.simulations.reporting import (
    SIMULATION_EPOCH,
    SimulationScenarioReport,
    SimulationSuiteReport,
    canonical_json,
    render_junit,
    render_markdown,
    write_report_files,
)


def _report(tmp_path: Path, *, secret: bool = False) -> SimulationSuiteReport:
    scenario = SimulationScenario.model_validate(
        {
            "schema_version": 1,
            "id": "reporting-check",
            "title": "Reporting | check\nrow",
            "seed": 7,
            "customers": ["acme"],
            "steps": [
                {
                    "id": "brief",
                    "type": "run_skill",
                    "skill": "account-brief",
                    "input": {"customer_id": "acme"},
                }
            ],
            "expectations": [
                {"type": "output_present", "path": "customer_id"},
            ],
        }
    )
    with SimulationWorld.create(tmp_path / "world", SIMULATION_EPOCH, scenario.seed) as world:
        run = JourneyRunner(world).run(scenario)
    if secret:
        step = run.steps[0].model_copy(
            update={
                "error_message": "owner=a@example.test password=hunter2 token=sk-" + "A" * 32,
                "artifacts": (
                    {
                        "filename": "secret.md",
                        "media_type": "text/markdown",
                        "content": "c2VjcmV0IGFydGlmYWN0",
                    },
                ),
            }
        )
        run = run.model_copy(update={"steps": (step,)})
    grade = DeterministicGrader().grade(scenario, run)
    if secret:
        grade = grade.model_copy(
            update={
                "findings": (
                    grade.findings[0].model_copy(
                        update={"message": "email a@example.test password=hunter2"}
                    ),
                )
            }
        )
    result = SimulationScenarioReport(
        scenario_id=scenario.id,
        scenario_title=scenario.title,
        seed=scenario.seed,
        run=run,
        grade=grade,
        passed=run.success and grade.passed,
        replay_command="csaf simulate scenarios --scenario reporting-check --seed 7",
    )
    return SimulationSuiteReport(
        schema_version=1,
        started_at=SIMULATION_EPOCH,
        config={"epoch": SIMULATION_EPOCH.isoformat()},
        total=1,
        passed_count=1 if result.passed else 0,
        failed_count=0 if result.passed else 1,
        passed=result.passed,
        scenarios=(result,),
    )


def test_canonical_report_round_trips_and_is_byte_stable(tmp_path: Path) -> None:
    report = _report(tmp_path)

    first = canonical_json(report)
    second = canonical_json(report)

    assert first == second
    assert first.endswith(b"\n")
    parsed = SimulationSuiteReport.model_validate_json(first)
    assert parsed.scenarios[0].scenario_id == report.scenarios[0].scenario_id


def test_human_serializers_escape_and_redact_without_mutating_report(tmp_path: Path) -> None:
    report = _report(tmp_path, secret=True)
    original = report.model_dump(mode="json")

    markdown = render_markdown(report)
    junit = render_junit(report)

    for rendered in (markdown, junit):
        assert "a@example.test" not in rendered
        assert "hunter2" not in rendered
        assert "secret artifact" not in rendered
        assert "content" not in rendered
        assert "Reporting | check" not in rendered
    assert "<redacted-email>" in markdown
    assert report.model_dump(mode="json") == original


def test_junit_uses_one_case_and_failure_for_each_failed_finding(tmp_path: Path) -> None:
    report = _report(tmp_path)
    scenario = report.scenarios[0]
    failed = scenario.grade.model_copy(
        update={
            "passed": False,
            "findings": (
                scenario.grade.findings[0].model_copy(
                    update={"passed": False, "message": "bad < & >"}
                ),
            ),
        }
    )
    report = report.model_copy(
        update={
            "passed": False,
            "passed_count": 0,
            "failed_count": 1,
            "scenarios": (
                scenario.model_copy(
                    update={
                        "run": scenario.run.model_copy(update={"success": False}),
                        "grade": failed,
                        "passed": False,
                    }
                ),
            ),
        }
    )

    xml = render_junit(report)

    assert xml.count("<testcase") == 1
    assert xml.count("<failure") == 2
    assert 'failures="2"' in xml
    assert "bad &lt; &amp; &gt;" in xml


def test_report_writer_is_atomic_and_rejects_unsafe_targets(tmp_path: Path) -> None:
    report = _report(tmp_path)
    destination = tmp_path / "reports"

    paths = write_report_files(report, destination)

    assert tuple(path.name for path in paths) == (
        "simulation-report.json",
        "simulation-report.md",
        "simulation-junit.xml",
    )
    assert all(path.exists() for path in paths)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="report directory is unsafe"):
        write_report_files(report, blocked)
