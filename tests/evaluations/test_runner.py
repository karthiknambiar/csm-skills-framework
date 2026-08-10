"""Golden-dataset and deterministic evaluation runner tests."""

import json
from pathlib import Path

import pytest

from csaf.evaluations import EvaluationRunner, load_golden_cases
from csaf.evaluations.loader import GoldenDatasetError
from csaf.evaluations.runner import _EvaluationOfficeRenderer
from csaf.evaluations.types import EvaluationCategory
from csaf.office import OfficeFormat, OfficeRenderRequest, OfficeSection


def test_bundled_golden_dataset_passes_all_regressions() -> None:
    cases = load_golden_cases("evaluations/golden")

    report = EvaluationRunner().run(cases)

    assert [case.skill_name for case in cases] == [
        "account-brief",
        "meeting-copilot",
        "qbr",
    ]
    assert report.passed is True
    assert report.cases_passed == report.cases_total == 3
    assert report.pass_rate == 1.0
    assert all(score == 1.0 for result in report.results for score in result.scores.values())


def test_evaluation_renderer_serializes_the_office_request() -> None:
    request = OfficeRenderRequest(
        format=OfficeFormat.POWERPOINT,
        title="Acme QBR",
        sections=(
            OfficeSection(
                title="Goals",
                bullets=("Expand regional adoption.",),
                citations=("memory:goal-1",),
            ),
        ),
    )

    rendered = _EvaluationOfficeRenderer().render(request)

    assert rendered == request.model_dump_json().encode("utf-8")


def test_bundled_evaluation_does_not_invoke_officecli_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_preflight(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("evaluation must not invoke OfficeCLI preflight")

    monkeypatch.setattr(
        "csaf.office.OfficeCLIDoctor.preflight",
        fail_preflight,
    )

    report = EvaluationRunner().run(load_golden_cases("evaluations/golden"))

    assert report.passed is True
    assert report.cases_total == 3


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
                    "executive_summary": "intentionally different",
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
