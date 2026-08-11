"""CLI transport tests using a persistent temporary SQLite database."""

# ruff: noqa: E402 -- optional transport dependency is checked before imports

import importlib
import json
import sys
from pathlib import Path, PurePosixPath

import pytest

typer = pytest.importorskip("typer")
from typer.testing import CliRunner

from csaf.cli import app
from csaf.memory import SQLiteMemoryStore
from csaf.office import (
    DiagnosticCheck,
    DiagnosticStatus,
    OfficeCLIArtifactRenderer,
    OfficeCLIConfig,
    OfficeCLIDoctor,
    OfficeCLIError,
    OfficeDiagnosticReport,
    OfficeRenderRequest,
)
from csaf.schemas import MemoryKind, MemoryQuery, MemoryRecordCreate

runner = CliRunner()


class StubOfficeRenderer:
    def render(self, request: OfficeRenderRequest) -> bytes:
        return f"rendered:{request.format.value}".encode()


def use_stub_office_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    cli_app = importlib.import_module("csaf.cli.app")
    original_create_runtime = cli_app.create_runtime
    monkeypatch.setattr(
        cli_app,
        "create_runtime",
        lambda database: original_create_runtime(database, office_renderer=StubOfficeRenderer()),
    )


def test_skills_list_returns_json(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"

    result = runner.invoke(app, ["--database", str(database), "skills", "list"])

    assert result.exit_code == 0
    assert [skill["name"] for skill in json.loads(result.stdout)] == [
        "account-brief",
        "meeting-copilot",
        "qbr",
    ]


def test_account_brief_writes_markdown_artifact(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"
    output = tmp_path / "nested" / "brief.md"

    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "account-brief",
            "acme",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["output"]["customer_id"] == "acme"
    assert output.read_text().startswith("# Account Brief: acme")


def test_account_brief_delivery_failure_does_not_write_memory(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked")

    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "account-brief",
            "acme",
            "--output",
            str(blocked_parent / "brief.md"),
        ],
    )

    assert result.exit_code == 2
    assert result.stderr.startswith("Error:")
    assert "Traceback" not in result.output
    with SQLiteMemoryStore(database) as memory:
        assert memory.history("acme", "account-brief:last-generated") == []


def test_meeting_analyze_reads_transcript_and_writes_artifact(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.md"
    transcript.write_text("Alex: Action: send the rollout plan.")
    output = tmp_path / "nested" / "analysis.md"

    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "meeting",
            "analyze",
            str(transcript),
            "--customer-id",
            "acme",
            "--meeting-id",
            "meeting-1",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["output"]["action_items"][0]["speaker"] == "Alex"
    assert output.read_text().startswith("# Meeting Analysis: meeting-1")


def test_meeting_delivery_failure_does_not_write_effects(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.md"
    transcript.write_text("Alex: Action: send the rollout plan.")
    database = tmp_path / "memory.db"
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked")

    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "meeting",
            "analyze",
            str(transcript),
            "--customer-id",
            "acme",
            "--meeting-id",
            "meeting-1",
            "--output",
            str(blocked_parent / "analysis.md"),
        ],
    )

    assert result.exit_code == 2
    assert result.stderr.startswith("Error:")
    assert "Traceback" not in result.output
    with SQLiteMemoryStore(database) as memory:
        assert memory.history("acme", "meeting:meeting-1") == []
        assert memory.search(MemoryQuery(customer_id="acme")) == []


def test_deliver_artifacts_rejects_unsafe_filename(tmp_path: Path) -> None:
    from csaf.cli.artifacts import deliver_artifacts
    from csaf.skills import Artifact, ArtifactType

    artifact = Artifact(
        type=ArtifactType.MARKDOWN,
        filename="../escape.md",
        media_type="text/markdown",
        content=b"unsafe",
    )

    with pytest.raises(OSError, match="unsafe artifact filename"):
        deliver_artifacts((artifact,), {artifact.filename: tmp_path / "escape.md"})

    assert not (tmp_path / "escape.md").exists()


def test_deliver_artifacts_rejects_unsafe_unselected_filename(tmp_path: Path) -> None:
    from csaf.cli.artifacts import deliver_artifacts
    from csaf.skills import Artifact, ArtifactType

    artifact = Artifact(
        type=ArtifactType.MARKDOWN,
        filename="../escape.md",
        media_type="text/markdown",
        content=b"unsafe",
    )

    with pytest.raises(OSError, match="unsafe artifact filename"):
        deliver_artifacts((artifact,), {})


def test_deliver_artifacts_rejects_backslash_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from csaf.cli import artifacts as artifact_delivery
    from csaf.skills import Artifact, ArtifactType

    monkeypatch.setattr(artifact_delivery, "Path", PurePosixPath)
    artifact = Artifact(
        type=ArtifactType.MARKDOWN,
        filename=r"nested\escape.md",
        media_type="text/markdown",
        content=b"unsafe",
    )

    with pytest.raises(OSError, match="unsafe artifact filename"):
        artifact_delivery.deliver_artifacts((artifact,), {})

    assert not (tmp_path / "escape.md").exists()


def test_deliver_artifacts_cleans_temp_file_after_staging_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from csaf.cli import artifacts as artifact_delivery
    from csaf.skills import Artifact, ArtifactType

    artifact = Artifact(
        type=ArtifactType.MARKDOWN,
        filename="brief.md",
        media_type="text/markdown",
        content=b"brief",
    )

    def fail_fsync(file_descriptor: int) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(artifact_delivery.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="disk unavailable"):
        artifact_delivery.deliver_artifacts(
            (artifact,), {artifact.filename: tmp_path / "output" / artifact.filename}
        )

    assert list((tmp_path / "output").iterdir()) == []


def test_qbr_writes_artifacts_named_by_skill_after_imported_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "memory.db"
    with SQLiteMemoryStore(database) as memory:
        memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.QBR,
                logical_key="imported:qbr:2026-Q3",
                content="Imported QBR.",
                metadata={"quarter": "2026-Q3"},
            )
        )
    use_stub_office_renderer(monkeypatch)
    output_dir = tmp_path / "nested" / "qbr"

    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "qbr",
            "generate",
            "acme",
            "--quarter",
            "2026-Q3",
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["output"]["artifact_version"] == 2
    assert (output_dir / "acme-2026-Q3-qbr-v2.pptx").read_bytes() == b"rendered:powerpoint"
    assert (output_dir / "acme-2026-Q3-qbr-v2.docx").read_bytes() == b"rendered:word"


def test_qbr_second_replace_failure_cleans_temps_and_does_not_write_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from csaf.cli import artifacts as artifact_delivery

    use_stub_office_renderer(monkeypatch)
    real_replace = artifact_delivery.os.replace
    replacements = 0

    def fail_second_replace(source: Path, destination: Path) -> None:
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("second replacement failed")
        real_replace(source, destination)

    monkeypatch.setattr(artifact_delivery.os, "replace", fail_second_replace)
    database = tmp_path / "memory.db"
    output_dir = tmp_path / "qbr"

    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "qbr",
            "generate",
            "acme",
            "--quarter",
            "2026-Q3",
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 2
    assert result.stderr.startswith("Error: second replacement failed")
    assert "Traceback" not in result.output
    assert list(output_dir.glob("*.tmp")) == []
    with SQLiteMemoryStore(database) as memory:
        assert memory.history("acme", "qbr:2026-Q3") == []
        assert memory.search(MemoryQuery(customer_id="acme")) == []


def test_memory_inspect_reads_configured_database(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"
    with SQLiteMemoryStore(database) as memory:
        memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.COMMITMENT,
                content="Send the migration plan.",
            )
        )

    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "memory",
            "inspect",
            "acme",
            "--kind",
            "commitment",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["content"] == "Send the migration plan."


def test_skill_run_returns_nonzero_for_unknown_skill(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "skill",
            "run",
            "unknown",
            "--input",
            '{"customer_id":"acme"}',
        ],
    )

    assert result.exit_code == 2
    assert "skill is not registered: unknown" in result.stderr


def test_skill_run_reads_input_from_utf8_json_file(tmp_path: Path) -> None:
    input_file = tmp_path / "skill-input.json"
    input_file.write_text('{"customer_id":"acme"}', encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "skill",
            "run",
            "account-brief",
            "--input-file",
            str(input_file),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["output"]["customer_id"] == "acme"


def test_skill_run_requires_exactly_one_input_source(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "skill",
            "run",
            "account-brief",
        ],
    )

    assert result.exit_code == 2
    assert "provide exactly one of --input or --input-file" in result.stderr


def test_skill_run_rejects_two_input_sources(tmp_path: Path) -> None:
    input_file = tmp_path / "skill-input.json"
    input_file.write_text('{"customer_id":"acme"}', encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "skill",
            "run",
            "account-brief",
            "--input",
            '{"customer_id":"acme"}',
            "--input-file",
            str(input_file),
        ],
    )

    assert result.exit_code == 2
    assert "provide exactly one of --input or --input-file" in result.stderr


@pytest.mark.parametrize(
    ("raw_input", "expected_error"),
    [
        ("{not-json", "Expecting property name"),
        ('["not", "an", "object"]', "skill input must be a JSON object"),
    ],
)
def test_skill_run_rejects_invalid_input_file(
    tmp_path: Path, raw_input: str, expected_error: str
) -> None:
    input_file = tmp_path / "skill-input.json"
    input_file.write_text(raw_input, encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "skill",
            "run",
            "account-brief",
            "--input-file",
            str(input_file),
        ],
    )

    assert result.exit_code == 2
    assert expected_error in result.stderr
    assert "Traceback" not in result.output


def test_skill_run_reports_unreadable_input_file_without_traceback(
    tmp_path: Path,
) -> None:
    missing_input = tmp_path / "missing.json"

    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "skill",
            "run",
            "account-brief",
            "--input-file",
            str(missing_input),
        ],
    )

    assert result.exit_code == 2
    assert result.stderr.startswith("Error:")
    assert "Traceback" not in result.output


def test_skill_run_reports_non_utf8_input_file_without_traceback(
    tmp_path: Path,
) -> None:
    input_file = tmp_path / "skill-input.json"
    input_file.write_bytes(b"\xff")

    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "skill",
            "run",
            "account-brief",
            "--input-file",
            str(input_file),
        ],
    )

    assert result.exit_code == 2
    assert result.stderr.startswith("Error:")
    assert "Traceback" not in result.output


def test_skill_run_omits_artifact_content_by_default(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "skill",
            "run",
            "account-brief",
            "--input",
            '{"customer_id":"acme"}',
        ],
    )

    assert result.exit_code == 0
    assert "content" not in json.loads(result.stdout)["artifacts"][0]


def test_skill_run_can_include_artifact_content(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "skill",
            "run",
            "account-brief",
            "--input",
            '{"customer_id":"acme"}',
            "--include-artifact-content",
        ],
    )

    assert result.exit_code == 0
    assert "content" in json.loads(result.stdout)["artifacts"][0]


def test_skill_run_writes_all_artifacts_to_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stub_office_renderer(monkeypatch)
    output_dir = tmp_path / "nested" / "artifacts"

    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "skill",
            "run",
            "qbr",
            "--input",
            '{"customer_id":"acme","quarter":"2026-Q3"}',
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    filenames = [artifact["filename"] for artifact in payload["artifacts"]]
    assert filenames == [
        "acme-2026-Q3-qbr-v1.pptx",
        "acme-2026-Q3-qbr-v1.docx",
    ]
    assert (output_dir / filenames[0]).read_bytes() == b"rendered:powerpoint"
    assert (output_dir / filenames[1]).read_bytes() == b"rendered:word"


def test_skill_run_delivery_failure_does_not_write_memory(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"
    blocked_output_dir = tmp_path / "not-a-directory"
    blocked_output_dir.write_text("blocked")

    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "skill",
            "run",
            "account-brief",
            "--input",
            '{"customer_id":"acme"}',
            "--output-dir",
            str(blocked_output_dir),
        ],
    )

    assert result.exit_code == 2
    assert result.stderr.startswith("Error:")
    assert "Traceback" not in result.output
    with SQLiteMemoryStore(database) as memory:
        assert memory.history("acme", "account-brief:last-generated") == []


def test_connector_ingest_writes_memory_and_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "records.json"
    source.write_text('[{"id":"risk-1","kind":"risk","content":"Renewal risk."}]')
    database = tmp_path / "memory.db"
    checkpoint = tmp_path / "checkpoint.json"

    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "connector",
            "ingest",
            "json",
            str(source),
            "--customer-id",
            "acme",
            "--checkpoint-file",
            str(checkpoint),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["records_written"] == 1
    assert json.loads(checkpoint.read_text())["state"]["completed"] is True
    with SQLiteMemoryStore(database) as memory:
        assert memory.search(MemoryQuery(customer_id="acme", kinds=(MemoryKind.RISK,)))


def test_evaluate_writes_passing_regression_report(tmp_path: Path) -> None:
    report = tmp_path / "evaluation-report.json"

    result = runner.invoke(
        app,
        ["evaluate", "evaluations/golden", "--report", str(report)],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["pass_rate"] == 1.0
    assert json.loads(report.read_text())["passed"] is True


class StubOfficeDoctor:
    def run(self) -> OfficeDiagnosticReport:
        names = ("executable", "version", "powerpoint-smoke", "word-smoke")
        return OfficeDiagnosticReport(
            ready=True,
            checks=tuple(
                DiagnosticCheck(name=name, status=DiagnosticStatus.PASS, message=f"{name} passed")
                for name in names
            ),
        )


def test_office_doctor_emits_deterministic_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_app = importlib.import_module("csaf.cli.app")
    monkeypatch.setattr(cli_app, "OfficeCLIDoctor", StubOfficeDoctor)
    result = runner.invoke(
        app,
        ["--database", str(tmp_path / "memory.db"), "office", "doctor", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ready"] is True
    assert [check["name"] for check in payload["checks"]] == [
        "executable",
        "version",
        "powerpoint-smoke",
        "word-smoke",
    ]


def test_office_doctor_failure_has_guidance_and_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_app = importlib.import_module("csaf.cli.app")

    class MissingDoctor:
        def run(self) -> OfficeDiagnosticReport:
            names = ("version", "powerpoint-smoke", "word-smoke")
            return OfficeDiagnosticReport(
                ready=False,
                checks=(
                    DiagnosticCheck(
                        name="executable",
                        status=DiagnosticStatus.FAIL,
                        message="OfficeCLI executable was not found: officecli",
                    ),
                    *(
                        DiagnosticCheck(name=name, status=DiagnosticStatus.SKIP, message="skipped")
                        for name in names
                    ),
                ),
            )

    monkeypatch.setattr(cli_app, "OfficeCLIDoctor", MissingDoctor)
    result = runner.invoke(
        app,
        ["--database", str(tmp_path / "memory.db"), "office", "doctor"],
    )

    assert result.exit_code == 2
    assert "OfficeCLI installation" in result.stdout
    assert "https://github.com/iOfficeAI/OfficeCLI" in result.stdout
    assert "Traceback" not in result.output


def test_qbr_preflight_failure_avoids_memory_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_app = importlib.import_module("csaf.cli.app")
    output_dir = tmp_path / "qbr"
    database = tmp_path / "memory.db"

    def fail_preflight(_runtime: object) -> None:
        raise OSError("OfficeCLI 1.0.137 or newer is required")

    monkeypatch.setattr(cli_app, "_preflight_officecli", fail_preflight)
    result = runner.invoke(
        app,
        [
            "--database",
            str(database),
            "qbr",
            "generate",
            "acme",
            "--quarter",
            "2026-Q3",
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 2
    assert "OfficeCLI 1.0.137 or newer" in result.stderr
    assert not output_dir.exists()
    with SQLiteMemoryStore(database) as memory:
        assert memory.search(MemoryQuery(customer_id="acme")) == []


def test_qbr_renderer_failure_redacts_standalone_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_app = importlib.import_module("csaf.cli.app")
    original_create_runtime = cli_app.create_runtime
    provider_key = "sk" + "-" + "A" * 32
    private_key_body = "QUJD" + "REVGR0hJSktMTU5PUFFSU1RVVldYWVo="
    private_key = (
        "-----BEGIN "
        + "PRIVATE KEY-----\\n"
        + private_key_body
        + "\\n-----END "
        + "PRIVATE KEY-----"
    )

    class FailingRenderer:
        def render(self, request: OfficeRenderRequest) -> bytes:
            raise OfficeCLIError(f"render failed: {provider_key}\\n{private_key}")

    monkeypatch.setattr(
        cli_app,
        "create_runtime",
        lambda database: original_create_runtime(database, office_renderer=FailingRenderer()),
    )
    result = runner.invoke(
        app,
        [
            "--database",
            str(tmp_path / "memory.db"),
            "qbr",
            "generate",
            "acme",
            "--quarter",
            "2026-Q3",
            "--output-dir",
            str(tmp_path / "qbr"),
        ],
    )

    assert result.exit_code == 2
    assert "<redacted-secret>" in result.stderr
    assert provider_key not in result.output
    assert private_key_body not in result.output
    assert "END PRIVATE KEY" not in result.output


def test_office_doctor_json_redacts_spaced_and_unc_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_app = importlib.import_module("csaf.cli.app")
    private_folder = "Private" + " Folder"
    windows_path = "C:" + f"\\Users\\Alice\\{private_folder}\\report.pptx"
    unc_path = "\\\\" + f"server\\share\\{private_folder}\\slides.pptx"
    posix_path = f"/home/alice/{private_folder}/report.docx"
    renderer = OfficeCLIArtifactRenderer(OfficeCLIConfig(executable=sys.executable))
    doctor = OfficeCLIDoctor(renderer)
    monkeypatch.setattr(renderer, "_version", lambda: (1, 0, 137))

    def fail_render(_request: OfficeRenderRequest) -> bytes:
        raise OfficeCLIError(
            f"OfficeCLI validate failed; '{windows_path}'; {unc_path}; {posix_path}; retry"
        )

    monkeypatch.setattr(renderer, "render", fail_render)
    monkeypatch.setattr(cli_app, "OfficeCLIDoctor", lambda: doctor)
    result = runner.invoke(
        app,
        ["--database", str(tmp_path / "memory.db"), "office", "doctor", "--json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    messages = [check["message"] for check in payload["checks"]]
    assert any("OfficeCLI validate failed" in message for message in messages)
    assert sum(message.count("<redacted-path>") for message in messages) >= 6
    for sensitive in (
        windows_path,
        unc_path,
        posix_path,
        private_folder,
        "server",
        "share",
        "report.pptx",
        "slides.pptx",
        "report.docx",
        "Alice",
        "alice",
    ):
        assert sensitive not in result.stdout
