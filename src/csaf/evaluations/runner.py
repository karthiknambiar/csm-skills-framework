"""Deterministic evaluation runner for structured skill contracts."""

import json
from collections import Counter
from collections.abc import Callable
from time import perf_counter
from typing import Any

from csaf.core import Runtime, create_runtime
from csaf.evaluations.types import (
    EvaluationCase,
    EvaluationCategory,
    EvaluationFinding,
    EvaluationReport,
    EvaluationResult,
)

_VOLATILE_KEYS = {
    "completed_at",
    "created_at",
    "execution_id",
    "generated_at",
    "memory_record_id",
    "started_at",
    "updated_at",
}


class EvaluationRunner:
    """Run golden cases in isolated runtimes and calculate regression scores."""

    def __init__(self, runtime_factory: Callable[[], Runtime] = create_runtime) -> None:
        self._runtime_factory = runtime_factory

    def run(self, cases: tuple[EvaluationCase, ...]) -> EvaluationReport:
        results = tuple(self.run_case(case) for case in cases)
        passed = sum(result.passed for result in results)
        return EvaluationReport(
            passed=passed == len(results),
            cases_total=len(results),
            cases_passed=passed,
            results=results,
        )

    def run_case(self, case: EvaluationCase) -> EvaluationResult:
        started = perf_counter()
        first_runtime = self._runtime_factory()
        second_runtime = self._runtime_factory()
        try:
            self._seed(first_runtime, case)
            self._seed(second_runtime, case)
            first = first_runtime.runner.run(case.skill_name, case.input)
            second = second_runtime.runner.run(case.skill_name, case.input)
            output = first.output.model_dump(mode="json")
            second_output = second.output.model_dump(mode="json")
            findings: list[EvaluationFinding] = []
            completeness = self._completeness(case, output, findings)
            artifact_completeness = self._artifacts(case, first, findings)
            scores = {
                EvaluationCategory.ACCURACY: self._accuracy(case, output, findings),
                EvaluationCategory.COMPLETENESS: min(
                    completeness, artifact_completeness
                ),
                EvaluationCategory.HALLUCINATION: self._hallucination(case, output, findings),
                EvaluationCategory.CITATION_QUALITY: self._citations(case, output, findings),
                EvaluationCategory.CONSISTENCY: self._consistency(
                    output, second_output, findings
                ),
                EvaluationCategory.MEMORY_UPDATES: self._memory_updates(
                    case, first, findings
                ),
            }
            thresholds = {
                category: case.minimum_scores.get(category, 1.0)
                for category in EvaluationCategory
            }
            passed = all(
                scores[category] >= threshold
                for category, threshold in thresholds.items()
            )
            return EvaluationResult(
                case_name=case.name,
                skill_name=case.skill_name,
                passed=passed,
                scores=scores,
                findings=tuple(findings),
                duration_ms=(perf_counter() - started) * 1_000,
            )
        finally:
            first_runtime.memory.close()
            second_runtime.memory.close()

    @staticmethod
    def _seed(runtime: Runtime, case: EvaluationCase) -> None:
        for record in case.memory:
            runtime.memory.append(record)

    @staticmethod
    def _accuracy(
        case: EvaluationCase,
        output: dict[str, Any],
        findings: list[EvaluationFinding],
    ) -> float:
        if not case.expected_values:
            return 1.0
        passed = 0
        for path, expected in case.expected_values.items():
            actual = _resolve(output, path)
            matches = actual == expected
            passed += matches
            findings.append(
                EvaluationFinding(
                    category=EvaluationCategory.ACCURACY,
                    passed=matches,
                    message=f"{path}: expected {expected!r}, received {actual!r}",
                )
            )
        return passed / len(case.expected_values)

    @staticmethod
    def _completeness(
        case: EvaluationCase,
        output: dict[str, Any],
        findings: list[EvaluationFinding],
    ) -> float:
        if not case.required_output_paths:
            return 1.0
        passed = 0
        for path in case.required_output_paths:
            value = _resolve(output, path)
            present = value is not None and value != "" and value != [] and value != {}
            passed += present
            findings.append(
                EvaluationFinding(
                    category=EvaluationCategory.COMPLETENESS,
                    passed=present,
                    message=f"required output {path} {'is present' if present else 'is missing'}",
                )
            )
        return passed / len(case.required_output_paths)

    @staticmethod
    def _hallucination(
        case: EvaluationCase,
        output: dict[str, Any],
        findings: list[EvaluationFinding],
    ) -> float:
        if not case.forbidden_terms:
            return 1.0
        serialized = json.dumps(output, sort_keys=True).casefold()
        absent = 0
        for term in case.forbidden_terms:
            passed = term.casefold() not in serialized
            absent += passed
            findings.append(
                EvaluationFinding(
                    category=EvaluationCategory.HALLUCINATION,
                    passed=passed,
                    message=f"forbidden term {term!r} {'was absent' if passed else 'was present'}",
                )
            )
        return absent / len(case.forbidden_terms)

    @staticmethod
    def _citations(
        case: EvaluationCase,
        output: dict[str, Any],
        findings: list[EvaluationFinding],
    ) -> float:
        citations = _count_key(output, "memory_record_id") + _count_nonempty_key(
            output, "excerpt"
        )
        score = min(citations / case.minimum_citations, 1.0) if case.minimum_citations else 1.0
        findings.append(
            EvaluationFinding(
                category=EvaluationCategory.CITATION_QUALITY,
                passed=citations >= case.minimum_citations,
                message=f"found {citations} citations; required {case.minimum_citations}",
            )
        )
        return score

    @staticmethod
    def _consistency(
        first: dict[str, Any],
        second: dict[str, Any],
        findings: list[EvaluationFinding],
    ) -> float:
        matches = _canonical(first) == _canonical(second)
        findings.append(
            EvaluationFinding(
                category=EvaluationCategory.CONSISTENCY,
                passed=matches,
                message="repeated isolated runs produced equivalent structured output",
            )
        )
        return float(matches)

    @staticmethod
    def _memory_updates(
        case: EvaluationCase,
        result: Any,
        findings: list[EvaluationFinding],
    ) -> float:
        if not case.expected_memory_writes:
            return 1.0
        actual = Counter(record.kind for record in result.memory_updates)
        passed = 0
        for kind, minimum in case.expected_memory_writes.items():
            matches = actual[kind] >= minimum
            passed += matches
            findings.append(
                EvaluationFinding(
                    category=EvaluationCategory.MEMORY_UPDATES,
                    passed=matches,
                    message=f"{kind.value}: wrote {actual[kind]}; required at least {minimum}",
                )
            )
        return passed / len(case.expected_memory_writes)

    @staticmethod
    def _artifacts(
        case: EvaluationCase,
        result: Any,
        findings: list[EvaluationFinding],
    ) -> float:
        actual = tuple(artifact.type for artifact in result.artifacts)
        matches = actual == case.expected_artifacts
        findings.append(
            EvaluationFinding(
                category=EvaluationCategory.COMPLETENESS,
                passed=matches,
                message=f"artifact types: expected {case.expected_artifacts}, received {actual}",
            )
        )
        return float(matches)


def _resolve(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _count_key(value: Any, key: str) -> int:
    if isinstance(value, dict):
        return int(key in value) + sum(_count_key(item, key) for item in value.values())
    if isinstance(value, list):
        return sum(_count_key(item, key) for item in value)
    return 0


def _count_nonempty_key(value: Any, key: str) -> int:
    if isinstance(value, dict):
        own = int(bool(value.get(key)))
        return own + sum(_count_nonempty_key(item, key) for item in value.values())
    if isinstance(value, list):
        return sum(_count_nonempty_key(item, key) for item in value)
    return 0


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _canonical(item)
            for key, item in value.items()
            if key not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value
