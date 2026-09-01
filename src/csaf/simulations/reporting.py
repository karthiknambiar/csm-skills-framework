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
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?![A-Za-z0-9_-])"
)
_XML_INVALID = re.compile(r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]")


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
    replay_argv: tuple[str, ...] = ("csaf", "simulate")
    replay_command: str | None = None

    @field_validator("replay_argv")
    @classmethod
    def validate_replay_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item or _CONTROL.search(item) or _BEARER.search(item) or _JWT.search(item)
            for item in value
        ):
            raise ValueError("replay argv contains an unsafe locator")
        if value[:2] != ("csaf", "simulate"):
            raise ValueError("replay argv must invoke csaf simulate")
        return value

    @model_validator(mode="after")
    def validate_identity_and_outcome(self) -> SimulationScenarioReport:
        if self.run.scenario_id != self.scenario_id or self.grade.scenario_id != self.scenario_id:
            raise ValueError("scenario report ids must match")
        if self.run.seed != self.seed or self.grade.seed != self.seed:
            raise ValueError("scenario report seeds must match")
        if self.passed is not (self.run.success and self.grade.passed):
            raise ValueError("scenario report passed must match run and grade")
        return self


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
    redacted = redact_officecli_message(value)
    redacted = _BEARER.sub("Bearer <redacted-secret>", redacted)
    redacted = _JWT.sub("<redacted-secret>", redacted)
    return _EMAIL.sub("<redacted-email>", redacted)


def _xml_text(value: object) -> str:
    """Return redacted text restricted to XML 1.0-valid code points."""

    return _XML_INVALID.sub("�", _redact_text(str(value)))


def _content_summary(value: object) -> dict[str, object]:
    raw: bytes | None = None
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        try:
            padded = value + "=" * (-len(value) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            raw = value.encode("utf-8")
    if raw is None:
        return {"size": None, "sha256": None}
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _redact_value(value: object, *, key: str | None = None) -> object:
    """Build a redacted copy; this never mutates evidence or report models."""

    if key == "replay_argv":
        return list(value) if isinstance(value, tuple | list) else value
    if key is not None and _CONTENT_FIELD.fullmatch(key):
        return _content_summary(value)
    if key is not None and _SENSITIVE_FIELD.search(key):
        return "<redacted-secret>"
    if isinstance(value, BaseModel):
        return _redact_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for child_key, child_value in value.items():
            name = str(child_key)
            if _CONTENT_FIELD.fullmatch(name):
                summary = _content_summary(child_value)
                result.setdefault("sha256", summary["sha256"])
                result.setdefault("size", summary["size"])
                continue
            result[name] = _redact_value(child_value, key=name)
        return result
    if isinstance(value, tuple | list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redacted_payload(report: SimulationSuiteReport) -> dict[str, object]:
    payload = report.model_dump(mode="json")
    redacted = _redact_value(payload)
    if not isinstance(redacted, dict):  # Defensive guard for the serializer API.
        raise TypeError("report payload must be an object")
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
