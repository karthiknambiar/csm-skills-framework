"""Checks that documentation links and runnable examples remain valid."""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")
_RELEASE_TEXT_FILES = (
    Path("README.md"),
    Path("CONTRIBUTING.md"),
    Path("docs/installation.md"),
    Path("docs/officecli.md"),
    Path("docs/rest-api.md"),
    Path("docs/compatibility.md"),
)


def test_relative_markdown_links_resolve() -> None:
    markdown_files = [Path("README.md"), Path("CONTRIBUTING.md")]
    markdown_files.extend(sorted(Path("docs").glob("*.md")))
    markdown_files.extend(sorted(Path("examples").glob("*.md")))
    failures: list[str] = []
    for document in markdown_files:
        for target in _MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#"):
                continue
            relative = target.split("#", maxsplit=1)[0]
            if relative and not (document.parent / relative).resolve().exists():
                failures.append(f"{document}: {target}")
    assert failures == []


def test_release_documentation_is_clean_utf8() -> None:
    failures: list[str] = []
    for document in _RELEASE_TEXT_FILES:
        raw = document.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            failures.append(f"{document}: UTF-8 BOM")
        text = raw.decode("utf-8")
        for marker in ("\u00e2\u201d", "\u00c3", "\ufffd"):
            if marker in text:
                failures.append(f"{document}: mojibake marker {marker!r}")
    assert failures == []


@pytest.mark.parametrize(
    "script",
    [
        "examples/account_brief.py",
        "examples/meeting_copilot.py",
        "examples/ingest_json.py",
    ],
)
def test_python_example_runs(script: str) -> None:
    completed = subprocess.run(
        [sys.executable, script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip()


def test_release_metadata_matches_repository_and_build_tooling() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]

    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE"]
    assert project["urls"] == {
        "Documentation": "https://github.com/karthiknambiar/csm-skills-framework#readme",
        "Source": "https://github.com/karthiknambiar/csm-skills-framework",
    }
    dev_dependencies = set(project["optional-dependencies"]["dev"])
    assert "build>=1.2,<2" in dev_dependencies
    assert "httpx2>=2,<3" in dev_dependencies
    assert not any(dependency.startswith("httpx>=") for dependency in dev_dependencies)


def test_repository_contains_canonical_apache_license() -> None:
    license_text = Path("LICENSE").read_text(encoding="utf-8")

    assert license_text.lstrip().startswith(
        "Apache License\n                           Version 2.0, January 2004"
    )
    assert "http://www.apache.org/licenses/" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert "Copyright {yyyy} {name of copyright owner}" in license_text


def test_officecli_documentation_describes_supported_local_runtime() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    guide = Path("docs/officecli.md").read_text(encoding="utf-8")
    combined = f"{readme}\n{guide}"

    for required in (
        "iOfficeAI/OfficeCLI",
        "1.0.137",
        "1.0.143",
        "csaf office doctor",
        "csaf office doctor --json",
        "officecli create",
        "officecli batch",
        "officecli validate",
        "officecli view",
    ):
        assert required in combined

    lowered = combined.lower()
    assert "fully local" in lowered
    assert "deterministic" in lowered
    assert "explicit consent" in lowered
    assert "official manual install" in lowered
    assert "api key" in lowered
    assert "hosted model" in lowered


def test_readme_is_native_first_and_follows_the_required_order() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    nonblank_lines = [line for line in readme.splitlines() if line.strip()]

    assert len(nonblank_lines) <= 180
    required_in_order = (
        "CSAF turns local customer context",
        "OfficeCLI is mandatory for QBR",
        "## Install",
        "### Windows",
        "### macOS",
        "### Linux",
        "## Ask your assistant to install CSAF",
        "## Use CSAF in natural language",
        "### Account Brief",
        "### Meeting Copilot",
        "### QBR",
        "## Setup lifecycle",
        "## Troubleshooting",
        "## Privacy",
        "## Advanced documentation",
        "## Development",
    )
    positions = [readme.index(fragment) for fragment in required_in_order]
    assert positions == sorted(positions)
    assert readme.index("## Development") > readme.index("## Use CSAF in natural language")


def test_readme_prominently_marks_native_integration_as_testing() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    notice = (
        "> **Testing status:** CSAF’s native agent integration is still being tested. "
        "Use non-production data, review each setup plan before consenting, and report "
        "unexpected behavior."
    )

    notice_position = readme.index(notice)
    opening_position = readme.index("CSAF turns local customer context")
    officecli_position = readme.index("OfficeCLI is mandatory")
    install_position = readme.index("## Install")

    assert opening_position < notice_position < officecli_position
    assert notice_position < install_position


def test_readme_installers_are_tagged_and_consent_first() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    installer_urls = re.findall(
        r"https://github[.]com/karthiknambiar/csm-skills-framework/[^\s'\"`)]+install[.](?:ps1|sh)",
        readme,
    )

    assert len(installer_urls) == 3
    assert all("/releases/download/v0.1.0/" in url for url in installer_urls)
    assert all("/main/" not in url and "/latest/" not in url for url in installer_urls)
    assert "tagged testing prerelease `v0.1.0`" in readme
    assert "tagged stable `v0.1.0`" not in readme
    assert "Direct installation remains available while testing" in readme
    assert "stable pinned OfficeCLI 1.0.143" in readme
    assert "explicit consent" in readme
    assert "No API key" in readme
    assert (
        "each detected supported assistant type for the current user/configured environment"
        in readme
    )
    assert "Ask for my consent before installing OfficeCLI" in readme


def test_assistant_install_prompt_matches_bootstrap_dependency_boundary() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    prompt = readme.split("> Open the CSAF GitHub release", maxsplit=1)[1].split(
        "\n\n", maxsplit=1
    )[0]
    dependencies = json.loads(Path("installer/dependencies.json").read_text(encoding="utf-8"))
    uv_version = dependencies["uv"]["version"]
    python_version = dependencies["python"]["version"]
    powershell_installer = Path("installer/install.ps1").read_text(encoding="utf-8")
    shell_installer = Path("installer/install.sh").read_text(encoding="utf-8")
    manifest_properties = json.loads(
        Path("installer/release-manifest.schema.json").read_text(encoding="utf-8")
    )["properties"]

    assert dependencies["python"] == {"version": python_version}
    assert dependencies["uv"]["assets"]
    assert f'$UvVersion = "{uv_version}"' in powershell_installer
    assert f'$PythonVersion = "{python_version}"' in powershell_installer
    assert 'Invoke-Checked $UvPath @("python", "install", $PythonVersion)' in powershell_installer
    assert f'uv_version="{uv_version}"' in shell_installer
    assert f'python_version="{python_version}"' in shell_installer
    assert 'invoke_checked "$uv_path" python install "$python_version"' in shell_installer
    assert "uv" not in manifest_properties
    assert "python" not in manifest_properties
    assert "installer and release manifest from the tagged `v0.1.0` release" in prompt
    assert "manifest-declared CSAF and OfficeCLI assets" in prompt
    assert f"pinned, checksum-verified uv {uv_version} platform asset" in prompt
    assert f"uv-managed Python {python_version} dependency download" in prompt
    assert "Do not download anything else." in prompt
    assert "HTTPS dependency assets" not in prompt
    assert "use only assets from that tagged release" not in prompt


def test_readme_executes_tagged_installers_with_the_matching_version() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert '& $installer -Version "0.1.0"' in readme
    assert readme.count("sh /tmp/csaf-install.sh --version 0.1.0") == 2


def test_offline_guidance_requires_reachable_https_assets() -> None:
    installation = Path("docs/installation.md").read_text(encoding="utf-8")

    assert "A local manifest does not make its assets local" in installation
    assert "internal HTTPS mirror" in installation
    assert "regenerate and review the manifest" in installation.lower()
    assert "Asset URLs must remain HTTPS" in installation


def test_privacy_scope_distinguishes_csaf_from_assistant_providers() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "The CSAF runtime itself" in readme
    assert "Codex, Claude Code, or Gemini CLI may handle prompts and files" in readme
    assert "Review your assistant provider's data controls" in readme


def test_readme_covers_every_builtin_workflow_and_qbr_template_policy() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    for skill_name in ("Account Brief", "Meeting Copilot", "QBR"):
        section = readme.split(f"### {skill_name}", maxsplit=1)[1].split("\n### ", maxsplit=1)[0]
        assert '"' in section
    assert "your own PowerPoint and Word QBR templates" in readme
    assert "sourced, vetted, bundled generic QBR templates" in readme
    assert "never downloads a template at runtime" in readme


def test_detailed_native_guides_match_setup_contracts() -> None:
    installation = Path("docs/installation.md").read_text(encoding="utf-8")
    office = Path("docs/officecli.md").read_text(encoding="utf-8")
    tutorial = Path("docs/tutorial.md").read_text(encoding="utf-8")
    index = Path("docs/index.md").read_text(encoding="utf-8")
    combined = f"{installation}\n{office}\n{tutorial}\n{index}"

    for directory in (
        "%LOCALAPPDATA%\\CSAF",
        "~/Library/Application Support/CSAF",
        "~/.local/share/csaf",
        "~/.codex/skills",
    ):
        assert directory in installation
    for phrase in (
        "offline",
        "local manifest",
        "SHA-256",
        "csaf setup install",
        "csaf setup doctor",
        "csaf setup repair",
        "csaf setup check-update",
        "csaf setup update",
        "csaf setup uninstall",
        "Codex",
        "Claude Code",
        "Gemini CLI",
        "each detected supported assistant type for the current user/configured environment",
        "explicit consent",
        "never auto-installs",
        "notifies",
    ):
        assert phrase in combined
    assert "~/.gemini/skills" in installation
    assert "--gemini-only" in installation
    assert "does not install a Gemini CLI adapter" not in installation
    assert "one target per detected assistant type" in installation
    assert "every detected installation" not in combined.lower()
    assert "1.0.143" in office
    assert "official manual install" in office
    assert "No API key is required" in office
    assert "csaf setup doctor" in tutorial
    assert "your own QBR template" in tutorial
    assert "bundled, vetted generic QBR template" in tutorial
    assert "[Install CSAF](installation.md)" in index


@pytest.mark.parametrize(
    "command",
    [
        ("setup",),
        ("setup", "install"),
        ("setup", "doctor"),
        ("setup", "repair"),
        ("setup", "check-update"),
        ("setup", "update"),
        ("setup", "uninstall"),
        ("office", "doctor"),
        ("account-brief",),
        ("meeting", "analyze"),
        ("qbr", "generate"),
    ],
)
def test_published_local_commands_exist_in_typer_help(
    command: tuple[str, ...], tmp_path: Path
) -> None:
    documents = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "README.md",
            "docs/installation.md",
            "docs/officecli.md",
            "docs/tutorial.md",
        )
        if Path(path).exists()
    )
    command_pattern = r"csaf(?: --database [^\s]+)? " + re.escape(" ".join(command))
    assert re.search(command_pattern, documents)

    executable = Path(sys.executable).with_name("csaf.exe" if sys.platform == "win32" else "csaf")
    worktree_database = Path("csaf.db").resolve()
    assert not worktree_database.exists()
    arguments = [str(executable), *command, "--help"]
    if command[0] != "setup":
        arguments = [str(executable), "--database", ":memory:", *command, "--help"]
    completed = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Usage:" in completed.stdout
    assert not (tmp_path / "csaf.db").exists()
    assert not worktree_database.exists()


def test_security_and_migration_documentation_matches_public_contracts() -> None:
    rest = Path("docs/rest-api.md").read_text(encoding="utf-8").lower()
    compatibility = Path("docs/compatibility.md").read_text(encoding="utf-8").lower()
    contributing = Path("CONTRIBUTING.md").read_text(encoding="utf-8").lower()

    assert "host application" in rest
    assert "authentication" in rest and "authorization" in rest
    assert "do not expose" in rest
    assert "no built-in api authentication" in rest

    assert "action_item" in compatibility
    assert "commitment" in compatibility
    assert "argument-template" in compatibility
    assert "officeartifactrenderer" in compatibility

    assert 'uv pip install --python .\\.venv\\scripts\\python.exe -e ".[dev]"' in contributing
    assert "python scripts/check_secrets.py --worktree --tracked --history" in contributing
    assert "python -m build" in contributing


def test_release_text_files_end_with_newline_and_headings_are_spaced() -> None:
    for document in _RELEASE_TEXT_FILES:
        assert document.read_bytes().endswith(b"\n"), f"{document} must end with LF"

    compatibility = Path("docs/compatibility.md").read_text(encoding="utf-8")
    assert "\n\n## Hardening-release migrations\n" in compatibility
    assert "\n\n### Test client dependency\n" in compatibility


def test_legacy_officecli_migration_is_documented_as_temporarily_functional() -> None:
    guide = Path("docs/officecli.md").read_text(encoding="utf-8").lower()
    compatibility = Path("docs/compatibility.md").read_text(encoding="utf-8").lower()
    combined = f"{guide}\n{compatibility}"

    assert "functional" in combined
    assert "0.1.x" in combined
    assert "0.2.0" in combined
    assert "create_arguments" in combined
    assert "update_arguments" in combined
    assert "officeartifactrenderer" in combined
    assert "inject" in combined


def test_readme_matches_runtime_lifecycle_and_bundled_evaluations() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    meeting = readme.split("### Meeting Copilot", maxsplit=1)[1].split("### QBR", maxsplit=1)[0]

    assert "`action_item`" in meeting
    assert "`feature_request`" in meeting
    assert "Account Brief, Meeting Copilot, and QBR regressions" in readme
