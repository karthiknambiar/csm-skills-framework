from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugins" / "csaf" / "skills" / "csaf"
SKILL_MD = SKILL / "SKILL.md"


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"\A---\n(?P<body>.*?)\n---\n", text, re.DOTALL)
    assert match is not None, "SKILL.md must start with YAML frontmatter"
    fields: dict[str, str] = {}
    for line in match.group("body").splitlines():
        key, separator, value = line.partition(":")
        assert separator and key and value.strip(), f"invalid frontmatter line: {line!r}"
        fields[key] = value.strip().strip('"')
    return fields


def test_skill_frontmatter_is_discoverable_and_concise() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    fields = _frontmatter(text)

    assert set(fields) == {"name", "description"}
    assert fields["name"] == "csaf"
    assert fields["description"].startswith("Use when ")
    assert len(fields["name"]) <= 64
    assert len(fields["description"]) <= 1024
    description = fields["description"].casefold()
    for trigger in (
        "account brief",
        "meeting",
        "qbr",
        "customer-success memory",
        "csaf readiness",
        "officecli readiness",
    ):
        assert trigger in description
    assert len(text.splitlines()) < 500


def test_skill_links_resolve_one_level_deep() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", text)
    expected = {
        "references/account-brief.md",
        "references/meeting-copilot.md",
        "references/qbr.md",
        "references/troubleshooting.md",
    }
    assert expected <= set(links)
    for target in links:
        assert "://" not in target
        assert "\\" not in target
        assert (SKILL / target).is_file(), target


def test_skill_enforces_local_grounded_and_consent_invariants() -> None:
    corpus = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [SKILL_MD, *sorted((SKILL / "references").glob("*.md"))]
    ).casefold()

    assert "officecli is mandatory" in corpus
    assert "explicit consent" in corpus
    assert "do not install" in corpus
    assert "do not invent" in corpus
    assert "do not download" in corpus
    assert "bundled" in corpus and "qbr template" in corpus
    assert "no api key" in corpus
    assert "hosted ai" in corpus
    assert "do not upload" in corpus
    assert "only claim" in corpus and "exit code 0" in corpus and "exact local paths" in corpus


@pytest.mark.parametrize(
    "command",
    (
        "csaf setup doctor",
        "csaf setup repair",
        "csaf setup update",
        "csaf setup check-update",
        "csaf office doctor --json",
    ),
)
def test_troubleshooting_has_exact_commands(command: str) -> None:
    text = (SKILL / "references" / "troubleshooting.md").read_text(encoding="utf-8")
    assert f"`{command}`" in text


def test_bootstrap_guidance_is_reachable_without_installed_csaf() -> None:
    text = (SKILL / "references" / "troubleshooting.md").read_text(encoding="utf-8")
    assert "releases/latest/download/install.ps1" in text
    assert "releases/latest/download/install.sh" in text
    assert "<downloaded-install.ps1>" in text
    assert "<downloaded-install.sh>" in text
    assert "explicit consent" in text.casefold()
    assert "`csaf setup install`" not in text


def test_workflow_references_define_exact_launcher_argv_and_required_inputs() -> None:
    account = (SKILL / "references" / "account-brief.md").read_text(encoding="utf-8")
    meeting = (SKILL / "references" / "meeting-copilot.md").read_text(encoding="utf-8")
    qbr = (SKILL / "references" / "qbr.md").read_text(encoding="utf-8")

    assert '["account-brief", "<customer-id>"' in account
    assert "customer identifier" in account.casefold()
    assert (
        '["meeting", "analyze", "<transcript>", "--customer-id", "<customer-id>", '
        '"--meeting-id", "<meeting-id>"' in meeting
    )
    for field in ("transcript", "customer identifier", "meeting identifier"):
        assert field in meeting.casefold()
    assert (
        '["qbr", "generate", "<customer-id>", "--quarter", "<yyyy-qn>", '
        '"--output-dir", "<directory>"' in qbr
    )
    for field in ("customer identifier", "quarter", "output directory"):
        assert field in qbr.casefold()
    assert '["office", "doctor", "--json"]' in qbr
    assert "--powerpoint-template" in qbr
    assert "--word-template" in qbr
    assert "ask the user to attach or provide its exact local path" in qbr.casefold()
    assert "do not silently substitute the bundled template" in qbr.casefold()
    for reference in (account, qbr):
        lowered = reference.casefold()
        assert "customer memory" in lowered
        assert "approval" in lowered
    for reference in (account, meeting, qbr):
        lowered = reference.casefold()
        assert "pass this launcher argument array" in lowered
        assert '["csaf"' not in lowered


def test_native_metadata_contracts() -> None:
    plugin = json.loads((ROOT / "plugins" / "csaf" / ".claude-plugin" / "plugin.json").read_text())
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    openai = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert plugin["name"] == "csaf"
    assert plugin["version"] == "0.1.0"
    assert plugin["license"] == "Apache-2.0"
    assert marketplace["name"] == "csaf"
    assert marketplace["plugins"] == [
        {
            "name": "csaf",
            "source": "./plugins/csaf",
            "version": "0.1.0",
            "license": "Apache-2.0",
        }
    ]
    assert 'display_name: "CSAF"' in openai
    assert 'short_description: "Local customer-success briefs, meetings, and QBRs"' in openai
    assert "$csaf" in openai


def test_evaluation_records_are_sanitized_and_complete() -> None:
    for name, loaded in (("baseline.json", False), ("with-skill.json", True)):
        record = json.loads((ROOT / "evaluations" / "native-skill" / name).read_text())
        assert record["schema_version"] == 1
        assert record["skill_loaded"] is loaded
        assert len(record["scenarios"]) == 5
        for scenario in record["scenarios"]:
            assert set(scenario) == {"id", "prompt", "outcome", "failure_categories"}
            serialized = json.dumps(scenario).casefold()
            assert "api_key" not in serialized
            assert "authorization:" not in serialized
        if loaded:
            outcomes = {item["id"]: item["outcome"].casefold() for item in record["scenarios"]}
            for scenario_id in ("account-brief-no-cli", "qbr-install-without-bothering"):
                assert "customer memory" in outcomes[scenario_id]
                assert "approval" in outcomes[scenario_id]
