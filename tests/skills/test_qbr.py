"""End-to-end QBR tests with an injected Office renderer."""

import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from csaf.core import Runtime, create_runtime
from csaf.office import OfficeFormat, OfficeOperation, OfficeRenderRequest
from csaf.schemas import MemoryKind, MemoryRecordCreate
from csaf.skills.errors import SkillExecutionError
from csaf.templates.qbr import default_qbr_powerpoint, default_qbr_word


class RecordingRenderer:
    def __init__(self) -> None:
        self.requests: list[OfficeRenderRequest] = []

    def render(self, request: OfficeRenderRequest) -> bytes:
        self.requests.append(request)
        return f"{request.format.value}:{request.operation.value}:{request.title}".encode()


def seed_qbr_memory(runtime: Runtime) -> None:
    memory = runtime.memory
    for record in (
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.PRODUCT_USAGE,
            content="Weekly active users increased by 18%.",
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.SUPPORT,
            content="Median resolution time fell to 8 hours.",
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.PROFILE,
            content="Reduce onboarding time by 25%.",
            metadata={"topic": "business_outcome"},
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.ROADMAP,
            content="SSO rollout is planned for October.",
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.RISK,
            content="The data migration remains delayed.",
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.ACTION_ITEM,
            content="Schedule the security review.",
        ),
        MemoryRecordCreate(
            customer_id="acme",
            kind=MemoryKind.COMMITMENT,
            content="Complete migration validation next month.",
        ),
    ):
        memory.append(record)


def test_qbr_generates_cited_powerpoint_and_word_and_versions_memory() -> None:
    renderer = RecordingRenderer()
    runtime = create_runtime(office_renderer=renderer)
    try:
        seed_qbr_memory(runtime)

        result = runtime.runner.run("qbr", {"customer_id": "acme", "quarter": "2026-Q3"})

        assert result.output.artifact_version == 1
        assert result.output.adoption_trends[0].text.endswith("18%.")
        assert result.output.support_metrics[0].text.endswith("8 hours.")
        assert result.output.business_outcomes[0].text.startswith("Reduce onboarding")
        assert result.output.roadmap[0].memory_record_id
        assert result.output.recommendations[0].text.startswith("The data migration")
        assert [item.text for item in result.output.next_quarter_plan] == [
            "Schedule the security review.",
            "Complete migration validation next month.",
        ]
        assert runtime.skills.get("qbr").metadata.version == "1.1.0"
        assert [artifact.type.value for artifact in result.artifacts] == [
            "powerpoint",
            "word",
        ]
        serialized = result.model_dump(mode="json")
        assert isinstance(serialized["artifacts"][0]["content"], str)
        assert renderer.requests[0].sections[1].citations[0].startswith("memory:")
        with default_qbr_powerpoint() as powerpoint_template:
            assert renderer.requests[0].template_path == powerpoint_template
        with default_qbr_word() as word_template:
            assert renderer.requests[1].template_path == word_template
        assert runtime.memory.history("acme", "qbr:2026-Q3")[0].revision == 1
        assert len(result.memory_updates) == 3
        assert result.memory_updates[0].metadata["template_source"] == {
            "powerpoint": "bundled",
            "word": "bundled",
        }
        assert result.memory_updates[1].metadata["template_source"] == "bundled"
        assert result.memory_updates[2].metadata["template_source"] == "bundled"
    finally:
        runtime.memory.close()


def test_qbr_updates_existing_artifacts_and_increments_versions(tmp_path: Path) -> None:
    renderer = RecordingRenderer()
    runtime = create_runtime(office_renderer=renderer)
    try:
        first = runtime.runner.run("qbr", {"customer_id": "acme", "quarter": "2026-Q3"})
        existing_powerpoint = tmp_path / first.artifacts[0].filename
        existing_word = tmp_path / first.artifacts[1].filename
        existing_powerpoint.write_bytes(first.artifacts[0].content)
        existing_word.write_bytes(first.artifacts[1].content)

        second = runtime.runner.run(
            "qbr",
            {
                "customer_id": "acme",
                "quarter": "2026-Q3",
                "existing_powerpoint": existing_powerpoint,
                "existing_word": existing_word,
            },
        )

        assert second.output.artifact_version == 2
        assert second.output.powerpoint_operation is OfficeOperation.UPDATE
        assert second.output.word_operation is OfficeOperation.UPDATE
        assert renderer.requests[-2].format is OfficeFormat.POWERPOINT
        assert renderer.requests[-2].existing_path == existing_powerpoint
        assert renderer.requests[-2].template_path is None
        assert renderer.requests[-1].format is OfficeFormat.WORD
        assert renderer.requests[-1].existing_path == existing_word
        assert renderer.requests[-1].template_path is None
        assert [record.revision for record in runtime.memory.history("acme", "qbr:2026-Q3")] == [
            1,
            2,
        ]
        assert second.artifacts[0].filename.endswith("qbr-v2.pptx")
        assert second.memory_updates[0].metadata["template_source"] == {
            "powerpoint": "existing",
            "word": "existing",
        }
        assert second.memory_updates[1].metadata["template_source"] == "existing"
        assert second.memory_updates[2].metadata["template_source"] == "existing"
    finally:
        runtime.memory.close()


def test_user_powerpoint_template_overrides_only_powerpoint(tmp_path: Path) -> None:
    template = tmp_path / "customer.pptx"
    template.write_bytes(b"customer template")
    renderer = RecordingRenderer()
    runtime = create_runtime(office_renderer=renderer)
    try:
        result = runtime.runner.run(
            "qbr",
            {
                "customer_id": "acme",
                "quarter": "2026-Q3",
                "powerpoint_template": template,
            },
        )

        assert renderer.requests[0].template_path == template
        with default_qbr_word() as word_template:
            assert renderer.requests[1].template_path == word_template
        assert result.memory_updates[0].metadata["template_source"] == {
            "powerpoint": "user",
            "word": "bundled",
        }
        assert result.memory_updates[1].metadata["template_source"] == "user"
        assert result.memory_updates[2].metadata["template_source"] == "bundled"
    finally:
        runtime.memory.close()


def test_user_word_template_overrides_only_word(tmp_path: Path) -> None:
    template = tmp_path / "customer.docx"
    template.write_bytes(b"customer template")
    renderer = RecordingRenderer()
    runtime = create_runtime(office_renderer=renderer)
    try:
        result = runtime.runner.run(
            "qbr",
            {
                "customer_id": "acme",
                "quarter": "2026-Q3",
                "word_template": template,
            },
        )

        with default_qbr_powerpoint() as powerpoint_template:
            assert renderer.requests[0].template_path == powerpoint_template
        assert renderer.requests[1].template_path == template
        assert result.memory_updates[0].metadata["template_source"] == {
            "powerpoint": "bundled",
            "word": "user",
        }
        assert result.memory_updates[1].metadata["template_source"] == "bundled"
        assert result.memory_updates[2].metadata["template_source"] == "user"
    finally:
        runtime.memory.close()


def test_default_templates_are_not_modified_by_rendering() -> None:
    renderer = RecordingRenderer()
    runtime = create_runtime(office_renderer=renderer)
    with default_qbr_powerpoint() as powerpoint_template:
        powerpoint_before = hashlib.sha256(powerpoint_template.read_bytes()).hexdigest()
    with default_qbr_word() as word_template:
        word_before = hashlib.sha256(word_template.read_bytes()).hexdigest()
    try:
        runtime.runner.run("qbr", {"customer_id": "acme", "quarter": "2026-Q3"})
    finally:
        runtime.memory.close()

    with default_qbr_powerpoint() as powerpoint_template:
        assert hashlib.sha256(powerpoint_template.read_bytes()).hexdigest() == powerpoint_before
    with default_qbr_word() as word_template:
        assert hashlib.sha256(word_template.read_bytes()).hexdigest() == word_before


@pytest.mark.parametrize("format_name", ["powerpoint", "word"])
@pytest.mark.parametrize("failure", ["missing", "corrupt", "tampered"])
def test_invalid_bundled_template_fails_before_render_or_memory_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    format_name: str,
    failure: str,
) -> None:
    with default_qbr_powerpoint() as powerpoint, default_qbr_word() as word:
        source_paths = {"powerpoint": powerpoint, "word": word}
        for source in source_paths.values():
            shutil.copyfile(source, tmp_path / source.name)
        shutil.copyfile(powerpoint.with_name("provenance.json"), tmp_path / "provenance.json")
    invalid = tmp_path / source_paths[format_name].name
    if failure == "missing":
        invalid.unlink()
    elif failure == "corrupt":
        invalid.write_bytes(b"not an OOXML archive")
    else:
        with zipfile.ZipFile(invalid, "a") as archive:
            archive.writestr("customXml/csaf-tamper.txt", "tampered")

    monkeypatch.setattr("csaf.templates.qbr.files", lambda _: tmp_path)
    renderer = RecordingRenderer()
    runtime = create_runtime(office_renderer=renderer)
    try:
        with pytest.raises(SkillExecutionError, match="bundled QBR template"):
            runtime.runner.run("qbr", {"customer_id": "acme", "quarter": "2026-Q3"})
        assert renderer.requests == []
        assert runtime.memory.history("acme", "qbr:2026-Q3") == []
    finally:
        runtime.memory.close()


def test_packaged_defaults_are_valid_ooxml_archives() -> None:
    expected = (
        (default_qbr_powerpoint, "ppt/presentation.xml"),
        (default_qbr_word, "word/document.xml"),
    )
    for default_template, required_member in expected:
        with default_template() as path, zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            assert required_member in archive.namelist()


def test_bundled_templates_are_content_empty_reusable_style_shells() -> None:
    with default_qbr_powerpoint() as powerpoint, default_qbr_word() as word:
        with zipfile.ZipFile(powerpoint) as archive:
            slide_parts = sorted(
                name
                for name in archive.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            )
            visible_slide_text = []
            for name in slide_parts:
                root = ElementTree.fromstring(archive.read(name))
                visible_slide_text.extend(
                    (element.text or "").strip()
                    for element in root.iter()
                    if element.tag.endswith("}t") and (element.text or "").strip()
                )
        assert slide_parts == []
        assert visible_slide_text == []

        with zipfile.ZipFile(word) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
            body = next(element for element in document.iter() if element.tag.endswith("}body"))
            content_children = [child for child in body if not child.tag.endswith("}sectPr")]
            visible_word_text = []
            for name in archive.namelist():
                if name == "word/document.xml" or name.startswith(
                    ("word/header", "word/footer", "word/footnotes")
                ):
                    if not name.endswith(".xml"):
                        continue
                    root = ElementTree.fromstring(archive.read(name))
                    visible_word_text.extend(
                        (element.text or "").strip()
                        for element in root.iter()
                        if element.tag.endswith("}t") and (element.text or "").strip()
                    )
        assert content_children == []
        assert visible_word_text == []


def test_bundled_templates_preserve_deliberate_qbr_style_hierarchy() -> None:
    namespaces = {
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    }
    with default_qbr_powerpoint() as powerpoint, default_qbr_word() as word:
        with zipfile.ZipFile(powerpoint) as archive:
            theme = ElementTree.fromstring(archive.read("ppt/theme/theme1.xml"))
            color_scheme = theme.find("a:themeElements/a:clrScheme", namespaces)
            font_scheme = theme.find("a:themeElements/a:fontScheme", namespaces)
            assert color_scheme is not None
            assert font_scheme is not None
            accent1 = color_scheme.find("a:accent1/a:srgbClr", namespaces)
            accent2 = color_scheme.find("a:accent2/a:srgbClr", namespaces)
            assert accent1 is not None and accent1.attrib["val"] == "1F5A7A"
            assert accent2 is not None and accent2.attrib["val"] == "38A3A5"
            major_latin = font_scheme.find("a:majorFont/a:latin", namespaces)
            minor_latin = font_scheme.find("a:minorFont/a:latin", namespaces)
            assert major_latin is not None and major_latin.attrib["typeface"] == "Arial"
            assert minor_latin is not None and minor_latin.attrib["typeface"] == "Arial"

            master = ElementTree.fromstring(archive.read("ppt/slideMasters/slideMaster1.xml"))
            background_color = master.find("p:cSld/p:bg/p:bgPr/a:solidFill/a:srgbClr", namespaces)
            assert background_color is not None
            assert background_color.attrib["val"] == "F7F9FC"
            named_shapes = {
                element.attrib.get("name") for element in master.findall(".//p:cNvPr", namespaces)
            }
            assert "CSAF Accent Rail" in named_shapes

        with zipfile.ZipFile(word) as archive:
            styles = ElementTree.fromstring(archive.read("word/styles.xml"))
            style_by_id = {
                element.attrib[f"{{{namespaces['w']}}}styleId"]: element
                for element in styles.findall("w:style", namespaces)
            }
            expected = {
                "Normal": {"size": "22", "color": "243247"},
                "Title": {"size": "60", "color": "17365D", "bold": True},
                "Subtitle": {"size": "28", "color": "5B6573"},
                "Heading1": {"size": "36", "color": "1F5A7A", "bold": True},
                "ListBullet": {"size": "22", "color": "243247"},
                "Caption": {"size": "18", "color": "687586", "italic": True},
            }
            assert expected.keys() <= style_by_id.keys()
            for style_id, contract in expected.items():
                style = style_by_id[style_id]
                size = style.find("w:rPr/w:sz", namespaces)
                color = style.find("w:rPr/w:color", namespaces)
                assert size is not None
                assert size.attrib[f"{{{namespaces['w']}}}val"] == contract["size"]
                assert color is not None
                assert color.attrib[f"{{{namespaces['w']}}}val"] == contract["color"]
                if contract.get("bold"):
                    assert style.find("w:rPr/w:b", namespaces) is not None
                if contract.get("italic"):
                    assert style.find("w:rPr/w:i", namespaces) is not None


def test_bundled_templates_are_generic_and_provenance_tracks_derivation() -> None:
    forbidden = (
        "TechVision",
        "James Mitchell",
        "Confidential",
        "No table of contents entries found",
        "Update field to see table of contents",
        "流光背景图片",
    )
    with default_qbr_powerpoint() as powerpoint, default_qbr_word() as word:
        for template in (powerpoint, word):
            with zipfile.ZipFile(template) as archive:
                visible_xml = b"".join(
                    archive.read(name) for name in archive.namelist() if name.endswith(".xml")
                ).decode("utf-8", errors="ignore")
            assert all(value not in visible_xml for value in forbidden)

        provenance = json.loads(powerpoint.with_name("provenance.json").read_text("utf-8"))
        for kind, template in (("powerpoint", powerpoint), ("word", word)):
            record = provenance["templates"][kind]
            assert record["modifications"]
            assert record["upstream_sha256"] != record["bundled_sha256"]
            assert hashlib.sha256(template.read_bytes()).hexdigest() == record["bundled_sha256"]
