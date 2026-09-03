"""Stable, redacted report formats for deterministic simulation journeys."""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Annotated, Literal
from xml.etree import ElementTree

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    field_validator,
    model_validator,
)

from csaf.office.redaction import redact_officecli_message
from csaf.simulations.schema import SimulationGrade, SimulationRun

SIMULATION_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
"""The fixed UTC start instant used by CLI simulation worlds and reports."""

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_SENSITIVE_FIELD = re.compile(
    r"(?i)(?:password|secret|token|credential|authorization|api[_-]?key|content|base64)"
)
_CONTENT_FIELD = re.compile(r"(?i)(?:content|payload|base64|data)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_SENSITIVE_REPLAY_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|secret|token|credential|authorization|api[_-]?key)\s*[:=]\s*\S+"
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?![A-Za-z0-9_-])"
)
_XML_INVALID = re.compile(r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]")
_SURROGATE = re.compile(r"[\uD800-\uDFFF]")


class _FrozenJsonDict(dict[str, object]):
    """An immutable JSON object for report configuration metadata."""

    def _reject(self, *_: object, **__: object) -> None:
        raise TypeError("report JSON values are immutable")

    __setitem__ = _reject
    __delitem__ = _reject
    __ior__ = _reject
    clear = _reject
    pop = _reject
    popitem = _reject
    setdefault = _reject
    update = _reject


def _freeze_json(value: object) -> object:
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("report JSON floats must be finite")
    if isinstance(value, dict):
        return _FrozenJsonDict({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _dump_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _dump_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_dump_json(item) for item in value]
    return value


FrozenJsonObject = Annotated[
    dict[str, JsonValue],
    AfterValidator(_freeze_json),
    PlainSerializer(_dump_json, return_type=dict[str, JsonValue]),
]


class SimulationScenarioReport(BaseModel):
    """One scenario's deterministic execution, grade, and replay instruction."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scenario_id: str = Field(min_length=1)
    scenario_title: str = Field(min_length=1)
    seed: int
    run: SimulationRun
    grade: SimulationGrade
    passed: bool
    replay_argv: tuple[str, ...]

    @field_validator("replay_argv")
    @classmethod
    def validate_replay_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        try:
            validate_replay_argv_safety(value)
        except ValueError:
            raise ValueError("replay argv contains an unsafe locator")
        if len(value) < 3 or value[:2] != ("csaf", "simulate"):
            raise ValueError("replay argv must invoke csaf simulate")
        if value[2].startswith("-"):
            raise ValueError("replay argv must include a dataset locator")
        return value

    @model_validator(mode="after")
    def validate_identity_and_outcome(self) -> SimulationScenarioReport:
        if self.run.scenario_id != self.scenario_id or self.grade.scenario_id != self.scenario_id:
            raise ValueError("scenario report ids must match")
        if self.run.seed != self.seed or self.grade.seed != self.seed:
            raise ValueError("scenario report seeds must match")
        if self.passed is not (self.run.success and self.grade.passed):
            raise ValueError("scenario report passed must match run and grade")
        _validate_replay_argv_shape(self.replay_argv)
        if self.replay_argv[4] != self.scenario_id or self.replay_argv[6] != str(self.seed):
            raise ValueError("replay argv must match scenario identity")
        return self


def _validate_replay_argv_shape(value: tuple[str, ...]) -> None:
    required = ("csaf", "simulate")
    if (
        len(value) not in {7, 9}
        or value[:2] != required
        or value[2].startswith("-")
        or value[3] != "--scenario"
        or value[5] != "--seed"
        or (len(value) == 9 and value[7] != "--fixture-root")
    ):
        raise ValueError("replay argv must have exact simulate grammar")


class SimulationSuiteReport(BaseModel):
    """Strict canonical report for an ordered simulation suite execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    started_at: datetime
    config: FrozenJsonObject = Field(default_factory=dict)
    total: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    passed: bool
    scenarios: tuple[SimulationScenarioReport, ...]

    @model_validator(mode="after")
    def validate_totals(self) -> SimulationSuiteReport:
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("started_at must include a timezone")
        if self.total != len(self.scenarios):
            raise ValueError("report total must equal scenario count")
        passed_count = sum(result.passed for result in self.scenarios)
        if self.passed_count != passed_count or self.failed_count != self.total - passed_count:
            raise ValueError("report totals must match scenario outcomes")
        if self.passed is not all(result.passed for result in self.scenarios):
            raise ValueError("report passed must match scenario outcomes")
        return self


def _redact_text(value: str) -> str:
    redacted = redact_officecli_message(_sanitize_unicode(value))
    redacted = _BEARER.sub("Bearer <redacted-secret>", redacted)
    redacted = _JWT.sub("<redacted-secret>", redacted)
    return _EMAIL.sub("<redacted-email>", redacted)


def _safe_replay_argument(value: str) -> bool:
    """Reject replay data that report redaction would alter for secrecy reasons."""

    return not (
        not value
        or _CONTROL.search(value)
        or _SURROGATE.search(value)
        or _EMAIL.search(value)
        or _BEARER.search(value)
        or _JWT.search(value)
        or _SENSITIVE_REPLAY_ASSIGNMENT.search(value)
        or redact_officecli_message(value, redact_paths=False) != value
    )


def validate_replay_argv_safety(value: tuple[str, ...]) -> None:
    """Raise a sanitized error when replay evidence contains a secret-shaped value."""

    if any(not _safe_replay_argument(item) for item in value):
        raise ValueError("simulation replay configuration is unsafe")


def _xml_text(value: object) -> str:
    """Return redacted text restricted to XML 1.0-valid code points."""

    return _XML_INVALID.sub("�", _redact_text(str(value)))


def _sanitize_unicode(value: str) -> str:
    """Replace non-scalar surrogate code points before rendering evidence."""

    return _SURROGATE.sub("�", value)


def _invalid_integrity() -> dict[str, object]:
    return {"integrity": "invalid", "size": None, "sha256": None}


def _content_summary(value: object) -> dict[str, object]:
    raw: bytes | None = None
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        try:
            padded = value + "=" * (-len(value) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            return _invalid_integrity()
    if raw is None:
        return _invalid_integrity()
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _redact_value(value: object, *, key: str | None = None) -> object:
    """Build a redacted copy; this never mutates evidence or report models."""

    if key is not None and _CONTENT_FIELD.fullmatch(key):
        return _content_summary(value)
    if key is not None and _SENSITIVE_FIELD.search(key):
        return "<redacted-secret>"
    if isinstance(value, BaseModel):
        return _redact_value(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        items: list[tuple[str, object]] = []
        names: set[str] = set()
        for child_key, child_value in value.items():
            name = _redact_text(_sanitize_unicode(str(child_key)))
            if name in names:
                raise ValueError("report mapping keys collide after Unicode sanitization")
            names.add(name)
            items.append((name, child_value))
        content_values = [
            child_value for name, child_value in items if _CONTENT_FIELD.fullmatch(name)
        ]
        result: dict[str, object] = {}
        for name, child_value in items:
            if content_values and (
                _CONTENT_FIELD.fullmatch(name) or name.lower() in {"sha256", "size", "integrity"}
            ):
                continue
            result[name] = _redact_value(child_value, key=name)
        if content_values:
            summary = (
                _content_summary(content_values[0])
                if len(content_values) == 1
                else _invalid_integrity()
            )
            result.update(summary)
        return result
    if isinstance(value, tuple | list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redacted_payload(report: SimulationSuiteReport) -> dict[str, object]:
    payload = report.model_dump(mode="python")
    redacted = _redact_value(payload)
    if not isinstance(redacted, dict):  # Defensive guard for the serializer API.
        raise TypeError("report payload must be an object")
    scenarios = redacted.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != len(report.scenarios):
        raise TypeError("report scenarios must be an ordered list")
    for payload_scenario, report_scenario in zip(scenarios, report.scenarios, strict=True):
        if not isinstance(payload_scenario, dict):
            raise TypeError("report scenario payload must be an object")
        try:
            replay_argv = report_scenario.replay_argv
            validate_replay_argv_safety(replay_argv)
            _validate_replay_argv_shape(replay_argv)
        except ValueError:
            raise ValueError("report replay evidence is unsafe") from None
        payload_scenario["replay_argv"] = list(replay_argv)
    return redacted


def canonical_json(report: SimulationSuiteReport) -> bytes:
    """Serialize a report as stable UTF-8 JSON without unsafe evidence."""

    return (
        json.dumps(
            _redacted_payload(report), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def _table(value: object) -> str:
    text = _CONTROL.sub(" ", _redact_text(str(value))).replace("\r", "").replace("\n", "<br>")
    return (
        html.escape(text, quote=False).replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")
    )


def render_markdown(report: SimulationSuiteReport) -> str:
    """Render a concise human-readable report, keeping evidence out of the output."""

    payload = _redacted_payload(report)
    lines = [
        "# CSAF Simulation Report",
        "",
        f"Result: {'PASS' if payload['passed'] else 'FAIL'}",
        (
            f"Scenarios: {payload['passed_count']}/{payload['total']} passed; "
            f"{payload['failed_count']} failed"
        ),
        "",
        "| Scenario | Result |",
        "| --- | --- |",
    ]
    for scenario in payload["scenarios"]:
        assert isinstance(scenario, Mapping)
        lines.append(
            "| "
            + _table(f"{scenario['scenario_id']}: {scenario['scenario_title']}")
            + f" | {'PASS' if scenario['passed'] else 'FAIL'} |"
        )
    lines.extend(["", "| Scenario | Finding | Result |", "| --- | --- | --- |"])
    for scenario in payload["scenarios"]:
        assert isinstance(scenario, Mapping)
        grade = scenario["grade"]
        assert isinstance(grade, Mapping)
        for finding in grade["findings"]:
            assert isinstance(finding, Mapping)
            finding_text = f"{finding['code']}: {finding['message']}"
            lines.append(
                f"| {_table(scenario['scenario_id'])} | {_table(finding_text)} | "
                f"{'PASS' if finding['passed'] else 'FAIL'} |"
            )
    lines.append(
        "\n## Replay argv vectors\n\nEach array is an argv vector; do not execute it as shell text."
    )
    for scenario in payload["scenarios"]:
        assert isinstance(scenario, Mapping)
        lines.extend(
            [
                "",
                f"### {_table(scenario['scenario_id'])}",
                "```json",
                json.dumps(scenario["replay_argv"], ensure_ascii=False),
                "```",
            ]
        )
    return "\n".join(lines) + "\n"


def render_junit(report: SimulationSuiteReport) -> str:
    """Render deterministic JUnit XML with failures for execution and grading issues."""

    payload = _redacted_payload(report)
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": "csaf.simulations",
            "tests": str(payload["total"]),
            "failures": "0",
            "errors": "0",
        },
    )
    failure_count = 0
    for scenario in payload["scenarios"]:
        assert isinstance(scenario, Mapping)
        case = ElementTree.SubElement(
            suite,
            "testcase",
            {"classname": "csaf.simulations", "name": _xml_text(scenario["scenario_id"])},
        )
        run = scenario["run"]
        grade = scenario["grade"]
        assert isinstance(run, Mapping) and isinstance(grade, Mapping)
        if not run["success"]:
            failure_count += 1
            ElementTree.SubElement(
                case, "failure", {"type": _xml_text("execution")}
            ).text = _xml_text("simulation execution did not succeed")
        for finding in grade["findings"]:
            assert isinstance(finding, Mapping)
            if not finding["passed"] and finding["code"] != "execution":
                failure_count += 1
                ElementTree.SubElement(
                    case, "failure", {"type": _xml_text(finding["code"])}
                ).text = _xml_text(finding["message"])
    suite.set("failures", str(failure_count))
    ElementTree.indent(suite, space="  ")
    return ElementTree.tostring(suite, encoding="unicode", xml_declaration=False) + "\n"


def _safe_report_directory(report_dir: Path) -> Path:
    candidate = Path(report_dir).absolute()
    if ".." in Path(report_dir).parts:
        raise ValueError("report directory is unsafe")
    for parent in (candidate, *candidate.parents):
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise ValueError("report directory is unsafe")
    candidate.mkdir(parents=True, exist_ok=True)
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("report directory is unsafe")
    return candidate


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".simulation-report-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_report_files(report: SimulationSuiteReport, report_dir: Path) -> tuple[Path, Path, Path]:
    """Atomically write the three exact report files below one safe directory."""

    directory = _safe_report_directory(report_dir)
    json_path = directory / "simulation-report.json"
    markdown_path = directory / "simulation-report.md"
    junit_path = directory / "simulation-junit.xml"
    _atomic_write(json_path, canonical_json(report))
    _atomic_write(markdown_path, render_markdown(report).encode("utf-8"))
    _atomic_write(junit_path, render_junit(report).encode("utf-8"))
    return json_path, markdown_path, junit_path


__all__ = [
    "SIMULATION_EPOCH",
    "SimulationScenarioReport",
    "SimulationSuiteReport",
    "canonical_json",
    "render_junit",
    "render_markdown",
    "write_report_files",
]
