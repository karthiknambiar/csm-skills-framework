"""Deterministic, redacted simulation report coverage."""

import json
from pathlib import Path
from xml.etree import ElementTree

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
        replay_argv=(
            "csaf",
            "simulate",
            str(tmp_path / "scenarios"),
            "--scenario",
            scenario.id,
            "--seed",
            str(scenario.seed),
        ),
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
    assert "&lt;redacted-email&gt;" in markdown
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


def test_serializers_redact_authorization_bearer_and_jwt_recursively(tmp_path: Path) -> None:
    report = _report(tmp_path)
    secret = "Bearer abcdef.ghijkl.mnopqr"
    finding = (
        report.scenarios[0]
        .grade.findings[0]
        .model_copy(update={"message": f"Authorization: {secret}"})
    )
    step = (
        report.scenarios[0]
        .run.steps[0]
        .model_copy(update={"output": {"headers": {"AUTHORIZATION": secret}}})
    )
    scenario = report.scenarios[0].model_copy(
        update={
            "run": report.scenarios[0].run.model_copy(update={"steps": (step,)}),
            "grade": report.scenarios[0].grade.model_copy(update={"findings": (finding,)}),
        }
    )
    report = report.model_copy(update={"scenarios": (scenario,)})
    rendered = (canonical_json(report).decode(), render_markdown(report), render_junit(report))
    for value in rendered:
        assert secret not in value
        assert "abcdef.ghijkl.mnopqr" not in value
    assert "<redacted-secret>" in rendered[0]
    assert "&lt;redacted-secret&gt;" in rendered[1]


def test_nested_replay_argv_output_is_redacted_not_trusted_as_report_evidence(
    tmp_path: Path,
) -> None:
    report = _report(tmp_path)
    step = (
        report.scenarios[0]
        .run.steps[0]
        .model_copy(update={"output": {"replay_argv": ["api_key=hunter2"]}})
    )
    scenario = report.scenarios[0].model_copy(
        update={"run": report.scenarios[0].run.model_copy(update={"steps": (step,)})}
    )
    report = report.model_copy(update={"scenarios": (scenario,)})

    rendered = (canonical_json(report).decode(), render_markdown(report), render_junit(report))

    assert all("hunter2" not in value for value in rendered)


def test_nested_secret_shaped_mapping_keys_are_redacted(tmp_path: Path) -> None:
    report = _report(tmp_path)
    secret = "abcdef.ghijkl.mnopqr"
    step = (
        report.scenarios[0]
        .run.steps[0]
        .model_copy(update={"output": {f"Authorization: Bearer {secret}": "present"}})
    )
    scenario = report.scenarios[0].model_copy(
        update={"run": report.scenarios[0].run.model_copy(update={"steps": (step,)})}
    )
    report = report.model_copy(update={"scenarios": (scenario,)})

    rendered = (canonical_json(report).decode(), render_markdown(report), render_junit(report))

    assert all(secret not in value for value in rendered)


def test_canonical_json_rejects_mapping_key_collisions_after_redaction(tmp_path: Path) -> None:
    report = _report(tmp_path)
    step = (
        report.scenarios[0]
        .run.steps[0]
        .model_copy(
            update={
                "output": {
                    "Authorization: Bearer aaaa.bbbb.cccc": "first",
                    "Authorization: Bearer dddd.eeee.ffff": "second",
                }
            }
        )
    )
    scenario = report.scenarios[0].model_copy(
        update={"run": report.scenarios[0].run.model_copy(update={"steps": (step,)})}
    )

    with pytest.raises(ValueError, match="mapping keys collide"):
        canonical_json(report.model_copy(update={"scenarios": (scenario,)}))


def test_serializers_reject_model_copy_replay_argv_bypasses_without_echoing_them(
    tmp_path: Path,
) -> None:
    report = _report(tmp_path)
    unsafe = "api_key=hunter2"
    scenario = report.scenarios[0].model_copy(
        update={
            "replay_argv": (
                "csaf",
                "simulate",
                unsafe,
                "--scenario",
                "reporting-check",
                "--seed",
                "7",
            )
        }
    )
    report = report.model_copy(update={"scenarios": (scenario,)})

    for serializer in (canonical_json, render_markdown, render_junit):
        with pytest.raises(ValueError, match="report replay evidence is unsafe") as error:
            serializer(report)
        assert unsafe not in str(error.value)


@pytest.mark.parametrize(
    ("replay_scenario", "replay_seed"),
    [("other-scenario", 7), ("reporting-check", 8)],
)
def test_serializers_reject_model_copy_replay_identity_bypasses(
    tmp_path: Path, replay_scenario: str, replay_seed: int
) -> None:
    report = _report(tmp_path)
    scenario = report.scenarios[0].model_copy(
        update={
            "replay_argv": (
                "csaf",
                "simulate",
                str(tmp_path / "scenarios"),
                "--scenario",
                replay_scenario,
                "--seed",
                str(replay_seed),
            )
        }
    )
    report = report.model_copy(update={"scenarios": (scenario,)})

    for serializer in (canonical_json, render_markdown, render_junit):
        with pytest.raises(ValueError, match="report replay evidence is unsafe"):
            serializer(report)


def test_markdown_escapes_table_content_and_uses_safe_replay_fence(tmp_path: Path) -> None:
    report = _report(tmp_path)
    scenario = report.scenarios[0].model_copy(
        update={
            "scenario_title": "bad | <script> \\ `code`\nnext",
        }
    )
    markdown = render_markdown(report.model_copy(update={"scenarios": (scenario,)}))
    assert "bad | <script>" not in markdown
    assert "&lt;script&gt;" in markdown
    assert "bad \\|" in markdown
    assert "`code`" not in markdown
    fence = max((line for line in markdown.splitlines() if set(line) == {"`"}), key=len)
    assert fence == "```"
    assert "Each array is an argv vector" in markdown


@pytest.mark.parametrize(
    "replay_argv",
    [
        ("csaf", "simulate", "scenarios", "--seed", "7"),
        (
            "csaf",
            "simulate",
            "scenarios",
            "--scenario",
            "reporting-check",
            "--scenario",
            "reporting-check",
            "--seed",
            "7",
        ),
        ("csaf", "simulate", "scenarios", "--scenario", "other", "--seed", "7"),
        ("csaf", "simulate", "scenarios", "--scenario", "reporting-check", "--seed", "8"),
        (
            "csaf",
            "simulate",
            "scenarios",
            "--scenario",
            "reporting-check",
            "--scenario=other",
            "--seed",
            "7",
        ),
    ],
)
def test_scenario_report_requires_exact_replay_identity(
    tmp_path: Path, replay_argv: tuple[str, ...]
) -> None:
    report = _report(tmp_path)
    payload = report.scenarios[0].model_dump()
    payload["replay_argv"] = replay_argv

    with pytest.raises(ValueError, match="replay argv"):
        SimulationScenarioReport.model_validate(payload)


@pytest.mark.parametrize(
    "suffix",
    [
        ("--help",),
        ("--fixture-root", "fixtures", "--help"),
        ("--fixture-root", "fixtures", "--fixture-root", "other"),
    ],
)
def test_scenario_report_rejects_surplus_reordered_replay_arguments(
    tmp_path: Path, suffix: tuple[str, ...]
) -> None:
    report = _report(tmp_path)
    payload = report.scenarios[0].model_dump()
    payload["replay_argv"] = (*payload["replay_argv"], *suffix)

    with pytest.raises(ValueError, match="replay argv"):
        SimulationScenarioReport.model_validate(payload)


def test_scenario_report_rejects_reordered_replay_arguments(tmp_path: Path) -> None:
    report = _report(tmp_path)
    payload = report.scenarios[0].model_dump()
    payload["replay_argv"] = (
        "csaf",
        "simulate",
        str(tmp_path / "scenarios"),
        "--seed",
        "7",
        "--scenario",
        "reporting-check",
    )

    with pytest.raises(ValueError, match="replay argv"):
        SimulationScenarioReport.model_validate(payload)


def test_canonical_json_removes_nested_artifact_payloads_with_integrity_summaries(
    tmp_path: Path,
) -> None:
    report = _report(tmp_path)
    encoded = "c2VjcmV0IGFydGlmYWN0"
    artifact = {
        "type": "artifact",
        "filename": "secret.md",
        "media_type": "text/markdown",
        "content": encoded,
        "nested": {"base64": encoded},
    }
    malformed = {
        "type": "artifact",
        "filename": "broken.bin",
        "media_type": "application/octet-stream",
        "content": "%%%not-base64%%%",
        "sha256": "reported-digest",
    }
    snapshot = report.scenarios[0].run.initial_snapshot.model_copy(
        update={"artifacts": (artifact, malformed)}
    )
    step = (
        report.scenarios[0]
        .run.steps[0]
        .model_copy(update={"artifacts": (artifact,), "before": snapshot, "after": snapshot})
    )
    run = report.scenarios[0].run.model_copy(
        update={
            "steps": (step,),
            "initial_snapshot": snapshot,
            "final_snapshot": snapshot,
            "artifacts": (artifact,),
        }
    )
    scenario = report.scenarios[0].model_copy(update={"run": run})
    report_with_artifacts = report.model_copy(update={"scenarios": (scenario,)})
    original = report_with_artifacts.model_dump(mode="json")
    redacted = json.loads(canonical_json(report_with_artifacts))

    assert encoded not in json.dumps(redacted)
    assert report_with_artifacts.model_dump(mode="json") == original
    expected = {
        "filename": "secret.md",
        "media_type": "text/markdown",
        "nested": {
            "sha256": "4036bf69a091b47f036f00a807e20af7201bccb6e2a13e418852a3fd3a8b88aa",
            "size": 15,
        },
        "sha256": "4036bf69a091b47f036f00a807e20af7201bccb6e2a13e418852a3fd3a8b88aa",
        "size": 15,
        "type": "artifact",
    }
    for location in (
        redacted["scenarios"][0]["run"]["artifacts"][0],
        redacted["scenarios"][0]["run"]["steps"][0]["artifacts"][0],
        redacted["scenarios"][0]["run"]["initial_snapshot"]["artifacts"][0],
    ):
        assert location == expected
    broken = redacted["scenarios"][0]["run"]["initial_snapshot"]["artifacts"][1]
    assert broken == {
        "filename": "broken.bin",
        "integrity": "invalid",
        "media_type": "application/octet-stream",
        "sha256": None,
        "size": None,
        "type": "artifact",
    }


@pytest.mark.parametrize(
    "artifact",
    [
        {
            "type": "artifact",
            "filename": "ordered.bin",
            "media_type": "application/octet-stream",
            "sha256": "attacker-sha",
            "size": 999,
            "integrity": "attacker-integrity",
            "content": "c2VjcmV0IGFydGlmYWN0",
        },
        {
            "content": "c2VjcmV0IGFydGlmYWN0",
            "size": 999,
            "sha256": "attacker-sha",
            "integrity": "attacker-integrity",
            "type": "artifact",
            "filename": "reversed.bin",
            "media_type": "application/octet-stream",
        },
    ],
)
def test_artifact_content_integrity_is_computed_not_supplied(
    tmp_path: Path, artifact: dict[str, object]
) -> None:
    report = _report(tmp_path)
    run = report.scenarios[0].run.model_copy(update={"artifacts": (artifact,)})
    scenario = report.scenarios[0].model_copy(update={"run": run})
    payload = json.loads(canonical_json(report.model_copy(update={"scenarios": (scenario,)})))

    summary = payload["scenarios"][0]["run"]["artifacts"][0]
    assert summary["sha256"] == "4036bf69a091b47f036f00a807e20af7201bccb6e2a13e418852a3fd3a8b88aa"
    assert summary["size"] == 15
    assert "content" not in summary
    assert "integrity" not in summary


def test_serializers_replace_surrogates_in_values_and_mapping_keys(tmp_path: Path) -> None:
    report = _report(tmp_path)
    surrogate = chr(0xD800)
    finding = (
        report.scenarios[0].grade.findings[0].model_copy(update={"message": "finding\ud800 ok"})
    )
    step = (
        report.scenarios[0]
        .run.steps[0]
        .model_copy(update={"output": {"nested\ud800key": "value\ud800", "valid": "snowman ☃"}})
    )
    scenario = report.scenarios[0].model_copy(
        update={
            "scenario_title": "title\ud800 ok",
            "run": report.scenarios[0].run.model_copy(update={"steps": (step,)}),
            "grade": report.scenarios[0].grade.model_copy(update={"findings": (finding,)}),
        }
    )
    step = step.model_copy(
        update={
            "output": {
                "nested" + surrogate + "key": "value" + surrogate,
                "valid": "snowman " + chr(0x2603),
            }
        }
    )
    scenario = scenario.model_copy(
        update={"run": scenario.run.model_copy(update={"steps": (step,)})}
    )
    report = report.model_copy(update={"scenarios": (scenario,)})

    rendered = (canonical_json(report).decode(), render_markdown(report), render_junit(report))

    assert all("\ud800" not in value for value in rendered)
    assert "nested�key" in rendered[0]
    assert "snowman ☃" in rendered[0]
    ElementTree.fromstring(rendered[2])


def test_canonical_json_rejects_mapping_key_collisions_after_surrogate_replacement(
    tmp_path: Path,
) -> None:
    report = _report(tmp_path)
    surrogate = chr(0xD800)
    step = report.scenarios[0].run.steps[0].model_copy(update={"output": {"placeholder": "first"}})
    scenario = report.scenarios[0].model_copy(
        update={"run": report.scenarios[0].run.model_copy(update={"steps": (step,)})}
    )
    step = step.model_copy(update={"output": {"key" + surrogate: "first", "key�": "second"}})
    scenario = scenario.model_copy(
        update={"run": scenario.run.model_copy(update={"steps": (step,)})}
    )

    with pytest.raises(ValueError, match="mapping keys collide"):
        canonical_json(report.model_copy(update={"scenarios": (scenario,)}))


def test_junit_replaces_invalid_xml_controls_in_attributes_and_text(tmp_path: Path) -> None:
    report = _report(tmp_path)
    finding = (
        report.scenarios[0]
        .grade.findings[0]
        .model_copy(update={"code": "bad\x01code", "message": "bad\x02message"})
    )
    scenario = report.scenarios[0].model_copy(
        update={
            "scenario_title": "bad\x04title",
            "grade": report.scenarios[0].grade.model_copy(update={"findings": (finding,)}),
        }
    )
    report = report.model_copy(update={"scenarios": (scenario,)})
    xml = render_junit(report)
    ElementTree.fromstring(xml)
    assert "\x01" not in xml and "\x02" not in xml


def test_writer_cleans_temporary_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csaf.simulations.reporting as reporting

    report = _report(tmp_path)
    destination = tmp_path / "reports"
    monkeypatch.setattr(
        reporting.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("blocked"))
    )
    with pytest.raises(OSError, match="blocked"):
        write_report_files(report, destination)
    assert list(destination.glob(".simulation-report-*")) == []


def test_writer_rejects_symlink_report_directory_when_supported(tmp_path: Path) -> None:
    report = _report(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")
    with pytest.raises(ValueError, match="report directory is unsafe"):
        write_report_files(report, link)
