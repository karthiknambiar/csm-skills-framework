"""Contract tests for the selected local iOfficeAI/OfficeCLI adapter."""

import json
import sys
from pathlib import Path

import pytest

from csaf.office import (
    OfficeCLIArtifactRenderer,
    OfficeCLIConfig,
    OfficeCLIError,
    OfficeFormat,
    OfficeRenderRequest,
    OfficeSection,
)


@pytest.fixture
def fake_officecli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    bridge = tmp_path / "fake_officecli.py"
    calls = tmp_path / "calls.jsonl"
    bridge.write_text(
        """
import json
import os
import pathlib
import sys
import time

arguments = sys.argv[1:]
command = arguments[0]
record = {
    "arguments": arguments,
    "flush": os.environ.get("OFFICECLI_RESIDENT_FLUSH"),
}
with pathlib.Path(os.environ["FAKE_OFFICECLI_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")

if os.environ.get("FAKE_OFFICECLI_SLEEP") == command:
    time.sleep(1)
if os.environ.get("FAKE_OFFICECLI_FAIL") == command:
    print(f"{command} exploded", file=sys.stderr)
    raise SystemExit(7)
if os.environ.get("FAKE_OFFICECLI_INVALID_UTF8") == command:
    stream = (
        sys.stderr.buffer
        if os.environ.get("FAKE_OFFICECLI_INVALID_STREAM") == "stderr"
        else sys.stdout.buffer
    )
    stream.write(bytes((0x81, 0xFF)))
    stream.flush()
    raise SystemExit(7)
if command == "--version":
    print(os.environ.get("FAKE_OFFICECLI_VERSION", "OfficeCLI v1.0.137"))
elif command == "create":
    pathlib.Path(arguments[1]).write_bytes(b"created")
elif command == "batch":
    document = pathlib.Path(arguments[1])
    batch = pathlib.Path(arguments[arguments.index("--input") + 1]).read_text(encoding="utf-8")
    document.write_bytes(document.read_bytes() + b"|batch:" + batch.encode())
    operations = json.loads(batch)
    count = len(operations)
    receipt = {
        "results": [
            {"index": index, "success": True}
            for index, _operation in enumerate(operations)
        ],
        "summary": {
            "total": count,
            "executed": count,
            "succeeded": count,
            "failed": 0,
            "skipped": 0,
        },
    }
    print(os.environ.get(
        "FAKE_OFFICECLI_BATCH",
        json.dumps({"success": True, "data": receipt}),
    ))
elif command == "validate":
    if os.environ.get("FAKE_OFFICECLI_RAW_UTF8") == "validate":
        payload = {
            "success": True,
            "data": {
                "count": 1,
                "errors": [{
                    "type": "Contenu",
                    "description": "déploiement 東京",
                    "path": "/word/document.xml",
                    "part": "résumé",
                }],
            },
        }
        sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        print(os.environ.get(
            "FAKE_OFFICECLI_VALIDATE",
            '{"success": true, "data": {"count": 0, "errors": []}}',
        ))
elif command == "view":
    print(os.environ.get(
        "FAKE_OFFICECLI_ISSUES",
        '{"success": true, "data": {"Count": 0, "Issues": []}}',
    ))
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_OFFICECLI_CALLS", str(calls))
    return bridge, calls


def _renderer(bridge: Path, **overrides: object) -> OfficeCLIArtifactRenderer:
    values: dict[str, object] = {
        "executable": sys.executable,
        "prefix_arguments": (str(bridge),),
    }
    values.update(overrides)
    return OfficeCLIArtifactRenderer(OfficeCLIConfig(**values))  # type: ignore[arg-type]


def _calls(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _request(format: OfficeFormat = OfficeFormat.WORD) -> OfficeRenderRequest:
    return OfficeRenderRequest(
        format=format,
        title="Acme QBR",
        sections=(OfficeSection(title="Summary", bullets=("On track",)),),
    )


def test_officecli_config_has_selected_local_defaults() -> None:
    assert OfficeCLIConfig() == OfficeCLIConfig(
        executable="officecli",
        prefix_arguments=(),
        timeout_seconds=120.0,
        minimum_version=(1, 0, 137),
    )


def test_powerpoint_create_uses_version_create_batch_validate_and_view(
    fake_officecli: tuple[Path, Path],
) -> None:
    bridge, calls_path = fake_officecli

    content = _renderer(bridge).render(
        OfficeRenderRequest(
            format=OfficeFormat.POWERPOINT,
            title='Acme "Enterprise" QBR',
            subtitle="2026 Q3",
            sections=(
                OfficeSection(
                    title="Summary",
                    bullets=("On track", "No shell $(interpolation)"),
                    citations=("memory:risk:1",),
                ),
            ),
        )
    )

    calls = _calls(calls_path)
    assert [call["arguments"][0] for call in calls] == [
        "--version",
        "create",
        "batch",
        "validate",
        "view",
    ]
    assert all(call["flush"] == "each" for call in calls)
    assert calls[2]["arguments"][2] == "--input"
    assert calls[2]["arguments"][4:] == ["--json"]
    assert calls[4]["arguments"][2:] == ["issues", "--json"]
    payload = json.loads(content.split(b"|batch:", 1)[1])
    assert payload[0] == {
        "command": "add",
        "parent": "/",
        "type": "slide",
        "props": {"title": 'Acme "Enterprise" QBR', "layout": "title"},
    }
    assert any(
        item["props"].get("text", "").endswith("No shell $(interpolation)") for item in payload
    )


def test_word_create_builds_deterministic_title_sections_bullets_and_citations(
    fake_officecli: tuple[Path, Path],
) -> None:
    bridge, _ = fake_officecli
    request = OfficeRenderRequest(
        format=OfficeFormat.WORD,
        title="Acme QBR",
        subtitle="2026 Q3",
        sections=(
            OfficeSection(
                title="Risks",
                bullets=("Adoption is delayed",),
                citations=("memory:risk:1", "memory:risk:2"),
            ),
        ),
    )

    first = _renderer(bridge).render(request)
    second = _renderer(bridge).render(request)

    first_batch = first.split(b"|batch:", 1)[1]
    assert first_batch == second.split(b"|batch:", 1)[1]
    payload = json.loads(first_batch)
    assert payload[0] == {
        "command": "add",
        "parent": "/body",
        "type": "paragraph",
        "props": {"text": "Acme QBR", "style": "Title"},
    }
    assert any(item["props"].get("text") == "2026 Q3" for item in payload)
    assert any(item["props"].get("text") == "Risks" for item in payload)
    assert any(item["props"].get("text") == "Adoption is delayed" for item in payload)
    assert any(
        item["props"].get("text") == "Sources: memory:risk:1; memory:risk:2" for item in payload
    )


@pytest.mark.parametrize("source_kind", ["template", "existing"])
def test_source_is_copied_and_never_mutated(
    fake_officecli: tuple[Path, Path], tmp_path: Path, source_kind: str
) -> None:
    bridge, calls_path = fake_officecli
    source = tmp_path / f"source-{source_kind}.docx"
    original = b"exact user document bytes"
    source.write_bytes(original)
    values = (
        {"template_path": source}
        if source_kind == "template"
        else {"operation": "update", "existing_path": source}
    )

    content = _renderer(bridge).render(
        OfficeRenderRequest(
            format=OfficeFormat.WORD,
            title="Updated QBR",
            sections=(OfficeSection(title="Summary", bullets=("Updated",)),),
            **values,
        )
    )

    assert source.read_bytes() == original
    assert content.startswith(original + b"|batch:")
    calls = _calls(calls_path)
    assert [call["arguments"][0] for call in calls] == [
        "--version",
        "batch",
        "validate",
        "view",
    ]
    assert Path(calls[1]["arguments"][1]) != source


def test_officecli_adapter_rejects_outdated_version(
    fake_officecli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, calls_path = fake_officecli
    monkeypatch.setenv("FAKE_OFFICECLI_VERSION", "officecli 1.0.136")

    with pytest.raises(OfficeCLIError, match=r"1\.0\.137 or newer.*1\.0\.136"):
        _renderer(bridge).render(_request())

    assert [call["arguments"][0] for call in _calls(calls_path)] == ["--version"]


@pytest.mark.parametrize(
    ("environment", "value", "message", "expected_commands"),
    [
        (
            "FAKE_OFFICECLI_VALIDATE",
            (
                '{"success": true, "data": {"count": 1, "errors": ['
                '{"type": "Package", "description": "broken relationship", '
                '"path": "/ppt/slides/slide1.xml", "part": "slide1"}]}}'
            ),
            "validation failed.*broken relationship",
            ["--version", "create", "batch", "validate"],
        ),
        (
            "FAKE_OFFICECLI_ISSUES",
            (
                '{"success": true, "data": {"Count": 1, "Issues": ['
                '{"Severity": "Error", "Message": "missing slide relationship", '
                '"Path": "/ppt/slides/slide1.xml"}]}}'
            ),
            "document issues.*missing slide relationship",
            ["--version", "create", "batch", "validate", "view"],
        ),
    ],
)
def test_officecli_adapter_rejects_invalid_artifacts(
    fake_officecli: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    value: str,
    message: str,
    expected_commands: list[str],
) -> None:
    bridge, calls_path = fake_officecli
    monkeypatch.setenv(environment, value)

    with pytest.raises(OfficeCLIError, match=message):
        _renderer(bridge).render(_request(OfficeFormat.POWERPOINT))

    assert [call["arguments"][0] for call in _calls(calls_path)] == expected_commands


def test_officecli_adapter_allows_nonfatal_document_issue(
    fake_officecli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _ = fake_officecli
    monkeypatch.setenv(
        "FAKE_OFFICECLI_ISSUES",
        (
            '{"success": true, "data": {"Count": 1, "Issues": ['
            '{"Severity": "Warning", "Message": "font substitution", '
            '"Path": "/word/document.xml"}]}}'
        ),
    )

    assert _renderer(bridge).render(_request()).startswith(b"created|batch:")


def _batch_receipt(
    *,
    total: int = 3,
    executed: int = 3,
    succeeded: int = 3,
    failed: int = 0,
    skipped: int = 0,
    atomic_rolled_back: bool = False,
    results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "results": (
            results
            if results is not None
            else [{"index": index, "success": True} for index in range(total)]
        ),
        "summary": {
            "total": total,
            "executed": executed,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
        },
    }
    if atomic_rolled_back:
        receipt["summary"]["atomicRolledBack"] = True  # type: ignore[index]
    return receipt


def test_officecli_adapter_accepts_standard_batch_envelope(
    fake_officecli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _ = fake_officecli
    monkeypatch.setenv(
        "FAKE_OFFICECLI_BATCH",
        json.dumps({"success": True, "data": _batch_receipt()}),
    )

    assert _renderer(bridge).render(_request()).startswith(b"created|batch:")


@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        "42",
        "{}",
        '{"success": false, "error": {"message": "atomic failure"}}',
        '{"success": true}',
        json.dumps({"success": True, "data": {"summary": {}, "results": []}}),
        json.dumps({"success": True, "data": {**_batch_receipt(), "results": []}}),
        json.dumps({"success": True, "data": _batch_receipt(failed=1, succeeded=2)}),
        json.dumps({"success": True, "data": _batch_receipt(skipped=1, executed=2, succeeded=2)}),
        json.dumps({"success": True, "data": _batch_receipt(atomic_rolled_back=True)}),
        json.dumps(
            {
                "success": True,
                "data": {**_batch_receipt(), "atomicRolledBack": False},
            }
        ),
        json.dumps(
            {
                "success": True,
                "data": {
                    **_batch_receipt(),
                    "summary": {
                        **_batch_receipt()["summary"],  # type: ignore[dict-item]
                        "atomicRolledBack": "true",
                    },
                },
            }
        ),
        json.dumps(
            {
                "success": True,
                "data": _batch_receipt(
                    results=[
                        {"index": 0, "success": True},
                        {"index": 1, "success": False},
                        {"index": 2, "success": True},
                    ]
                ),
            }
        ),
        json.dumps({"success": True, "data": _batch_receipt(total=4)}),
    ],
)
def test_officecli_adapter_rejects_unrecognized_or_incomplete_batch_receipt(
    fake_officecli: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    response: str,
) -> None:
    bridge, calls_path = fake_officecli
    monkeypatch.setenv("FAKE_OFFICECLI_BATCH", response)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: (_ for _ in ()).throw(AssertionError("artifact bytes were read")),
    )

    with pytest.raises(OfficeCLIError, match="batch"):
        _renderer(bridge).render(_request())

    assert [call["arguments"][0] for call in _calls(calls_path)] == [
        "--version",
        "create",
        "batch",
    ]


@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        "42",
        "{}",
        '{"success": false, "error": {"message": "validation unavailable"}}',
        '{"success": true}',
        '{"success": true, "data": {}}',
        '{"success": true, "data": {"count": "0", "errors": []}}',
        '{"success": true, "data": {"count": 0, "errors": {}}}',
    ],
)
def test_officecli_adapter_rejects_malformed_validation_response(
    fake_officecli: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    response: str,
) -> None:
    bridge, calls_path = fake_officecli
    monkeypatch.setenv("FAKE_OFFICECLI_VALIDATE", response)

    with pytest.raises(OfficeCLIError, match="validation"):
        _renderer(bridge).render(_request())

    assert [call["arguments"][0] for call in _calls(calls_path)] == [
        "--version",
        "create",
        "batch",
        "validate",
    ]


def test_officecli_adapter_preserves_official_validation_diagnostics(
    fake_officecli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _ = fake_officecli
    monkeypatch.setenv(
        "FAKE_OFFICECLI_VALIDATE",
        json.dumps(
            {
                "success": True,
                "data": {
                    "count": 1,
                    "errors": [
                        {
                            "type": "Package",
                            "description": "broken relationship",
                            "path": "/ppt/slides/slide1.xml",
                            "part": "slide1",
                        }
                    ],
                },
            }
        ),
    )

    with pytest.raises(
        OfficeCLIError,
        match=r"Package.*broken relationship.*/ppt/slides/slide1\.xml.*slide1",
    ):
        _renderer(bridge).render(_request())


@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        "42",
        "{}",
        '{"success": false, "error": {"message": "inspection failed"}}',
        '{"success": true}',
        '{"success": true, "data": {}}',
        '{"success": true, "data": {"Count": "0", "Issues": []}}',
        '{"success": true, "data": {"Count": 1, "Issues": []}}',
        '{"success": true, "data": {"Count": 0, "Issues": {}}}',
    ],
)
def test_officecli_adapter_rejects_malformed_issue_response(
    fake_officecli: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    response: str,
) -> None:
    bridge, calls_path = fake_officecli
    monkeypatch.setenv("FAKE_OFFICECLI_ISSUES", response)

    with pytest.raises(OfficeCLIError, match="issue inspection"):
        _renderer(bridge).render(_request())

    assert [call["arguments"][0] for call in _calls(calls_path)] == [
        "--version",
        "create",
        "batch",
        "validate",
        "view",
    ]


def test_officecli_adapter_preserves_raw_utf8_diagnostics(
    fake_officecli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _ = fake_officecli
    monkeypatch.setenv("FAKE_OFFICECLI_RAW_UTF8", "validate")

    with pytest.raises(
        OfficeCLIError,
        match="Contenu.*déploiement 東京.*/word/document.xml.*résumé",
    ):
        _renderer(bridge).render(_request())


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_officecli_adapter_safely_rejects_invalid_utf8_process_output(
    fake_officecli: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
) -> None:
    bridge, _ = fake_officecli
    monkeypatch.setenv("FAKE_OFFICECLI_INVALID_UTF8", "--version")
    monkeypatch.setenv("FAKE_OFFICECLI_INVALID_STREAM", stream)

    with pytest.raises(OfficeCLIError, match=r"--version.*not valid UTF-8") as captured:
        _renderer(bridge).render(_request())

    assert "\\x81" not in str(captured.value)
    assert isinstance(captured.value.__cause__, UnicodeError)


def test_officecli_adapter_reports_missing_executable() -> None:
    renderer = OfficeCLIArtifactRenderer(
        OfficeCLIConfig(executable="officecli-command-that-does-not-exist")
    )

    with pytest.raises(OfficeCLIError, match="was not found"):
        renderer.render(_request())


def test_officecli_adapter_reports_nonzero_command(
    fake_officecli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _ = fake_officecli
    monkeypatch.setenv("FAKE_OFFICECLI_FAIL", "batch")

    with pytest.raises(OfficeCLIError, match="batch.*exit code 7.*batch exploded"):
        _renderer(bridge).render(_request())


def test_officecli_adapter_reports_timeout(
    fake_officecli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _ = fake_officecli
    monkeypatch.setenv("FAKE_OFFICECLI_SLEEP", "--version")

    with pytest.raises(OfficeCLIError, match=r"--version.*0\.01s timeout"):
        _renderer(bridge, timeout_seconds=0.01).render(_request())


def test_officecli_adapter_rejects_missing_update_source(tmp_path: Path) -> None:
    renderer = OfficeCLIArtifactRenderer()

    with pytest.raises(OfficeCLIError, match="existing Office artifact was not found"):
        renderer.render(
            OfficeRenderRequest(
                format=OfficeFormat.WORD,
                operation="update",
                title="QBR",
                sections=(OfficeSection(title="Summary"),),
                existing_path=tmp_path / "missing.docx",
            )
        )


def test_officecli_adapter_rejects_missing_template(tmp_path: Path) -> None:
    renderer = OfficeCLIArtifactRenderer()

    with pytest.raises(OfficeCLIError, match="Office template was not found"):
        renderer.render(
            OfficeRenderRequest(
                format=OfficeFormat.POWERPOINT,
                title="QBR",
                sections=(OfficeSection(title="Summary"),),
                template_path=tmp_path / "missing.pptx",
            )
        )
