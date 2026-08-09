"""Golden-dataset and deterministic evaluation runner tests."""

import json
from pathlib import Path

import pytest

from csaf.evaluations import EvaluationRunner, load_golden_cases
from csaf.evaluations.loader import GoldenDatasetError
from csaf.evaluations.types import EvaluationCategory


def test_bundled_golden_dataset_passes_all_regressions() -> None:
    cases = load_golden_cases("evaluations/golden")

    report = EvaluationRunner().run(cases)

    assert [case.name for case in cases] == [
        "account-brief-grounded-risk",
        "meeting-copilot-actions-and-risk",
    ]
    assert report.passed is True
    assert report.cases_passed == report.cases_total == 2
    assert report.pass_rate == 1.0
    assert all(
        score == 1.0
        for result in report.results
        for score in result.scores.values()
    )


def test_runner_reports_actionable_accuracy_regression(tmp_path: Path) -> None:
    case_path = tmp_path / "regression.json"
    case_path.write_text(
        json.dumps(
            {
                "name": "intentional-regression",
                "skill_name": "account-brief",
                "input": {"customer_id": "acme"},
                "expected_values": {"customer_id": "wrong-customer"},
                "expected_memory_writes": {"artifact": 1},
                "expected_artifacts": ["markdown"],
            }
        )
    )

    result = EvaluationRunner().run(load_golden_cases(case_path)).results[0]

    assert result.passed is False
    assert result.scores[EvaluationCategory.ACCURACY] == 0.0
    assert any("wrong-customer" in finding.message for finding in result.findings)


def test_case_can_set_a_partial_regression_threshold(tmp_path: Path) -> None:
    case_path = tmp_path / "threshold.json"
    case_path.write_text(
        json.dumps(
            {
                "name": "partial-threshold",
                "skill_name": "account-brief",
                "input": {"customer_id": "acme"},
                "expected_values": {
                    "customer_id": "acme",
                    "executive_summary": "intentionally different"
                },
                "minimum_scores": {"accuracy": 0.5},
                "expected_memory_writes": {"artifact": 1},
                "expected_artifacts": ["markdown"],
            }
        )
    )

    result = EvaluationRunner().run(load_golden_cases(case_path)).results[0]

    assert result.scores[EvaluationCategory.ACCURACY] == 0.5
    assert result.passed is True


def test_loader_rejects_duplicate_case_names(tmp_path: Path) -> None:
    duplicate = {
        "name": "duplicate-case",
        "skill_name": "account-brief",
        "input": {"customer_id": "acme"},
    }
    (tmp_path / "cases.json").write_text(json.dumps([duplicate, duplicate]))

    with pytest.raises(GoldenDatasetError, match="must be unique"):
        load_golden_cases(tmp_path)
