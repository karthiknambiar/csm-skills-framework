"""Pure, deterministic hard graders for simulation evidence."""

import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence

from csaf.simulations.schema import (
    ArtifactTypesExpectation,
    CitationMinimumExpectation,
    ForbiddenTermExpectation,
    GradeFinding,
    MemoryCountExpectation,
    MemoryRevisionExpectation,
    NoCrossCustomerDataExpectation,
    NoPartialEffectsExpectation,
    OutputEqualsExpectation,
    OutputPresentExpectation,
    SimulationExpectation,
    SimulationGrade,
    SimulationRun,
    SimulationScenario,
    StepResult,
)

_MISSING = object()
_ARTIFACT_FORMATS = {
    "markdown": (".md", "text/markdown"),
    "word": (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "powerpoint": (
        ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    "excel": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "pdf": (".pdf", "application/pdf"),
    "html": (".html", "text/html"),
}


class DeterministicGrader:
    """Grade declared simulation expectations without I/O or model calls."""

    def grade(self, scenario: SimulationScenario, run: SimulationRun) -> SimulationGrade:
        """Return stable findings for the supplied, identity-matched evidence."""

        if run.scenario_id != scenario.id:
            raise ValueError("run scenario id does not match scenario")
        if run.seed != scenario.seed:
            raise ValueError("run seed does not match scenario")
        _require_finite(run.model_dump(mode="python"))

        findings = [
            self._grade_expectation(expectation, scenario, run)
            for expectation in scenario.expectations
        ]
        if not run.success:
            findings.append(
                GradeFinding(
                    code="execution",
                    passed=False,
                    message="simulation execution did not succeed",
                )
            )
        return SimulationGrade(
            scenario_id=scenario.id,
            seed=scenario.seed,
            passed=run.success and all(finding.passed for finding in findings),
            findings=tuple(findings),
        )

    def _grade_expectation(
        self,
        expectation: SimulationExpectation,
        scenario: SimulationScenario,
        run: SimulationRun,
    ) -> GradeFinding:
        try:
            if isinstance(expectation, OutputEqualsExpectation):
                value = _resolve(_target_output(expectation.step_id, run), expectation.path)
                passed = value is not _MISSING and _json_equal(value, expectation.value)
                message = "output value matched" if passed else "output value did not match"
            elif isinstance(expectation, OutputPresentExpectation):
                value = _resolve(_target_output(expectation.step_id, run), expectation.path)
                passed = value is not _MISSING and value not in (None, "", (), [], {})
                message = "output value is present" if passed else "output value is absent"
            elif isinstance(expectation, ForbiddenTermExpectation):
                haystack = _target_text(expectation.step_id, run)
                passed = expectation.term.casefold() not in haystack.casefold()
                message = "forbidden term is absent" if passed else "forbidden term is present"
            elif isinstance(expectation, MemoryCountExpectation):
                count = sum(
                    isinstance(record, Mapping)
                    and record.get("customer_id") == expectation.customer_id
                    for record in run.final_snapshot.memory
                )
                passed = count == expectation.count
                message = (
                    "memory record count matched" if passed else "memory record count did not match"
                )
            elif isinstance(expectation, MemoryRevisionExpectation):
                revisions = [
                    record.get("revision")
                    for record in run.final_snapshot.memory
                    if isinstance(record, Mapping)
                    and record.get("customer_id") == expectation.customer_id
                    and record.get("logical_key") == expectation.logical_key
                    and type(record.get("revision")) is int
                ]
                passed = bool(revisions) and max(revisions) == expectation.revision
                message = "memory revision matched" if passed else "memory revision did not match"
            elif isinstance(expectation, ArtifactTypesExpectation):
                artifacts = _target_artifacts(expectation.step_id, run)
                passed = _artifacts_match(artifacts, expectation.values)
                message = (
                    "artifact types and integrity matched" if passed else "artifact contract failed"
                )
            elif isinstance(expectation, CitationMinimumExpectation):
                count = _citation_count(_target_output(expectation.step_id, run))
                passed = count >= expectation.count
                message = "citation minimum met" if passed else "citation minimum not met"
            elif isinstance(expectation, NoCrossCustomerDataExpectation):
                passed = _no_cross_customer_data(expectation.step_id, scenario, run)
                message = (
                    "customer boundaries preserved" if passed else "customer boundary violation"
                )
            elif isinstance(expectation, NoPartialEffectsExpectation):
                passed = _no_partial_effects(expectation.step_id, run)
                message = (
                    "failed step rolled back exactly"
                    if passed
                    else "failed step effects were not exact"
                )
            else:  # pragma: no cover - schema discriminated union is exhaustive.
                raise TypeError("unsupported simulation expectation")
        except (TypeError, ValueError, UnicodeError):
            passed = False
            message = "evidence was malformed"
        return GradeFinding(
            code=expectation.type,
            passed=passed,
            message=message,
            step_id=expectation.step_id,
        )


def _target_step(step_id: str | None, run: SimulationRun) -> StepResult | None:
    if step_id is None:
        return None
    return next((step for step in run.steps if step.id == step_id), None)


def _target_output(step_id: str | None, run: SimulationRun) -> object:
    step = _target_step(step_id, run)
    return run.last_output if step_id is None else (_MISSING if step is None else step.output)


def _target_artifacts(step_id: str | None, run: SimulationRun) -> Sequence[Mapping[str, object]]:
    if step_id is None:
        value: object = run.artifacts
    else:
        step = _target_step(step_id, run)
        value = () if step is None else step.artifacts
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _target_text(step_id: str | None, run: SimulationRun) -> str:
    if step_id is None:
        return "\n".join(str(item) for item in run.serialized_outputs)
    value = _target_output(step_id, run)
    return _canonical_json(value) if value is not _MISSING else ""


def _resolve(value: object, path: str) -> object:
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, Sequence) and not isinstance(current, str | bytes | bytearray):
            if not part.isdigit() or int(part) >= len(current):
                return _MISSING
            current = current[int(part)]
        else:
            return _MISSING
    return current


def _json_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, Sequence) and not isinstance(left, str | bytes | bytearray):
        return (
            isinstance(right, Sequence)
            and len(left) == len(right)
            and all(_json_equal(one, two) for one, two in zip(left, right, strict=True))
        )
    return left == right


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _artifacts_match(artifacts: Sequence[Mapping[str, object]], expected: Sequence[str]) -> bool:
    if tuple(artifact.get("type") for artifact in artifacts) != tuple(expected):
        return False
    return all(_artifact_is_valid(artifact) for artifact in artifacts)


def _artifact_is_valid(artifact: Mapping[str, object]) -> bool:
    artifact_type = artifact.get("type")
    filename = artifact.get("filename")
    media_type = artifact.get("media_type")
    content = artifact.get("content")
    digest = artifact.get("sha256")
    if not all(
        isinstance(value, str) for value in (artifact_type, filename, media_type, content, digest)
    ):
        return False
    expected = _ARTIFACT_FORMATS.get(artifact_type)
    if expected is None or not _safe_basename(filename):
        return False
    extension, expected_media_type = expected
    if not filename.casefold().endswith(extension) or media_type != expected_media_type:
        return False
    try:
        decoded = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        return False
    if not decoded or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return False
    return hashlib.sha256(decoded).hexdigest() == digest


def _safe_basename(filename: str) -> bool:
    return (
        bool(filename)
        and filename not in {".", ".."}
        and not re.search(r"[\\/]", filename)
        and not re.match(r"^[A-Za-z]:", filename)
    )


def _citation_count(value: object) -> int:
    """Count unique identifiers, source values, and excerpts in traversal order."""

    seen: set[str] = set()

    def add(kind: str, candidate: object) -> int:
        if candidate in (None, "", (), [], {}):
            return 0
        try:
            key = f"{kind}:{_canonical_json(candidate)}"
        except (TypeError, ValueError):
            return 0
        if key in seen:
            return 0
        seen.add(key)
        return 1

    def walk(item: object) -> int:
        if isinstance(item, Mapping):
            count = add("memory", item.get("memory_record_id")) if "memory_record_id" in item else 0
            if "sources" in item:
                sources = item["sources"]
                values = sources.values() if isinstance(sources, Mapping) else sources
                if isinstance(values, Sequence) and not isinstance(values, str | bytes | bytearray):
                    count += sum(add("source", value) for value in values)
                else:
                    count += add("source", values)
            if "excerpt" in item:
                count += add("excerpt", item["excerpt"])
            return count + sum(walk(value) for value in item.values())
        if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            return sum(walk(value) for value in item)
        return 0

    return walk(value)


def _no_cross_customer_data(
    step_id: str | None, scenario: SimulationScenario, run: SimulationRun
) -> bool:
    customers = set(scenario.customers)
    all_targets = _target_steps_for_boundary(None, scenario, run)
    targets = tuple(
        (step, customer) for step, customer in all_targets if step_id is None or step.id == step_id
    )
    if not targets:
        return len(customers) == 1
    if len(customers) > 1 and any(customer is None for _, customer in targets):
        return False

    for step, customer in targets:
        if not _evidence_is_safe(step.output, customer, customers):
            return False
        if not _aggregate_is_safe(step.updates, ((step, customer),), customers):
            return False
        if not _artifacts_are_safe(step.artifacts, ((step, customer),), customers):
            return False

    updates = tuple((step, customer) for step, customer in all_targets for _ in step.updates)
    if not _aggregate_is_safe(run.updates, updates, customers, step_id):
        return False

    output_steps = tuple(
        (step, customer)
        for step, customer in all_targets
        if step.type == "run_skill" and step.success and not step.expected_error
    )
    if not _aggregate_is_safe(run.outputs, output_steps, customers, step_id):
        return False
    if not _serialized_outputs_are_safe(run.serialized_outputs, output_steps, customers, step_id):
        return False
    if run.last_output is not None:
        if not output_steps:
            if len(customers) > 1:
                return False
        elif step_id is None and not _evidence_is_safe(
            run.last_output, output_steps[-1][1], customers
        ):
            return False
        elif step_id == output_steps[-1][0].id and not _evidence_is_safe(
            run.last_output, output_steps[-1][1], customers
        ):
            return False

    artifacts = tuple((step, customer) for step, customer in all_targets for _ in step.artifacts)
    return _artifacts_are_safe(run.artifacts, artifacts, customers, step_id)


def _aggregate_is_safe(
    values: Sequence[object],
    origins: Sequence[tuple[StepResult, str | None]],
    customers: set[str],
    step_id: str | None = None,
) -> bool:
    if len(values) != len(origins):
        return len(customers) == 1 and all(
            _evidence_is_safe(value, next(iter(customers)), customers) for value in values
        )
    for value, (step, customer) in zip(values, origins, strict=True):
        if step_id is not None and step.id != step_id:
            continue
        if not _evidence_is_safe(value, customer, customers):
            return False
    return True


def _serialized_outputs_are_safe(
    values: Sequence[str],
    origins: Sequence[tuple[StepResult, str | None]],
    customers: set[str],
    step_id: str | None,
) -> bool:
    if len(values) != len(origins):
        if len(customers) > 1:
            return False
        customer = next(iter(customers))
        return all(
            _evidence_is_safe(_parse_canonical_json(value), customer, customers) for value in values
        )
    for value, (step, customer) in zip(values, origins, strict=True):
        if step_id is not None and step.id != step_id:
            continue
        if not _evidence_is_safe(_parse_canonical_json(value), customer, customers):
            return False
    return True


def _parse_canonical_json(value: str) -> object:
    def no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("serialized output has duplicate keys")
            result[key] = item
        return result

    def reject_non_finite(_: str) -> object:
        raise ValueError("serialized output has non-finite values")

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=no_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("serialized output is invalid") from error
    if _canonical_json(parsed) != value:
        raise ValueError("serialized output is not canonical")
    return parsed


def _artifacts_are_safe(
    artifacts: Sequence[object],
    origins: Sequence[tuple[StepResult, str | None]],
    customers: set[str],
    step_id: str | None = None,
) -> bool:
    if len(artifacts) != len(origins):
        if len(customers) > 1:
            return False
        origin = next(iter(customers))
        return all(_artifact_is_safe(artifact, origin, customers) for artifact in artifacts)
    for artifact, (step, customer) in zip(artifacts, origins, strict=True):
        if step_id is not None and step.id != step_id:
            continue
        if not _artifact_is_safe(artifact, customer, customers):
            return False
    return True


def _evidence_is_safe(value: object, customer: str | None, customers: set[str]) -> bool:
    if customer is None:
        return len(customers) == 1 and not _contains_customer_identifier(value, set())
    return not _contains_customer_identifier(value, customers - {customer}, customer)


def _artifact_is_safe(artifact: object, customer: str | None, customers: set[str]) -> bool:
    if not isinstance(artifact, Mapping) or not _evidence_is_safe(artifact, customer, customers):
        return False
    forbidden = set() if customer is None else customers - {customer}
    return not _artifact_text_contains_customer((artifact,), forbidden)


def _target_steps_for_boundary(
    step_id: str | None, scenario: SimulationScenario, run: SimulationRun
) -> tuple[tuple[StepResult, str | None], ...]:
    declared = {step.id: step for step in scenario.steps if step.id is not None}
    steps = (step for step in run.steps if step_id is None or step.id == step_id)
    result = []
    for step in steps:
        declared_step = declared.get(step.id)
        target = None
        input_value = getattr(declared_step, "input", None)
        if isinstance(input_value, Mapping) and isinstance(input_value.get("customer_id"), str):
            target = input_value["customer_id"]
        elif hasattr(declared_step, "customer_id"):
            candidate = getattr(declared_step, "customer_id")
            target = candidate if isinstance(candidate, str) else None
        elif len(scenario.customers) == 1:
            target = scenario.customers[0]
        result.append((step, target))
    return tuple(result)


def _contains_customer_identifier(
    value: object, identifiers: set[str], allowed_customer: str | None = None
) -> bool:
    if isinstance(value, Mapping):
        customer_id = value.get("customer_id")
        if customer_id is not None and customer_id != allowed_customer:
            return True
        return any(
            _contains_customer_identifier(item, identifiers, allowed_customer)
            for item in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return any(
            _contains_customer_identifier(item, identifiers, allowed_customer) for item in value
        )
    if isinstance(value, str):
        return bool(identifiers) and any(
            re.search(rf"(?<![A-Za-z0-9]){re.escape(item)}(?![A-Za-z0-9])", value, re.I)
            for item in identifiers
        )
    return False


def _artifact_text_contains_customer(
    artifacts: Sequence[Mapping[str, object]], identifiers: set[str]
) -> bool:
    for artifact in artifacts:
        content = artifact.get("content")
        if not isinstance(content, str):
            return True
        try:
            text = base64.b64decode(content, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError, ValueError):
            continue
        if _contains_customer_identifier(text, identifiers):
            return True
    return False


def _no_partial_effects(step_id: str, run: SimulationRun) -> bool:
    step = _target_step(step_id, run)
    if step is None or (step.success and not step.expected_error):
        return False
    return (
        step.before.memory == step.after.memory
        and step.before.artifacts == step.after.artifacts
        and step.before.office_requests == step.after.office_requests
    )


def _require_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("run evidence contains non-finite value")
    if isinstance(value, Mapping):
        for item in value.values():
            _require_finite(item)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            _require_finite(item)
