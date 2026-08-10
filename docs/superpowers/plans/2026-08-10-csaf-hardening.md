# CSAF Plug-and-Play Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CSAF's deterministic skills safe and practical for non-technical users through ordered artifact delivery, local OfficeCLI rendering, clearer outputs, evaluations, and secret protection.

**Architecture:** Preserve package boundaries. `SkillRunner` coordinates artifact delivery before memory commits, the CLI owns atomic filesystem writes, `iOfficeAI/OfficeCLI` stays behind `OfficeArtifactRenderer`, and deterministic skills gain a clearer memory taxonomy without hosted models or built-in API authentication.

**Tech Stack:** Python 3.11/3.12, Pydantic 2, Typer, FastAPI, SQLite, pytest, Ruff, Hatchling, local `iOfficeAI/OfficeCLI` subprocesses.

---

## File map

- `src/csaf/skills/{types,runner}.py`: artifact handler contract and ordering.
- `src/csaf/cli/{artifacts,app}.py`: atomic writes, JSON-file input, compact output, doctor command.
- `src/csaf/schemas/memory.py`: `action_item` memory kind.
- `src/csaf/skills/builtin/{meeting_copilot,account_brief,qbr}.py`: deterministic quality changes.
- `src/csaf/office/{officecli,diagnostics}.py`: selected local CLI adapter and preflight.
- `src/csaf/evaluations/runner.py`, `evaluations/golden/qbr.json`: QBR golden coverage.
- `scripts/check_secrets.py`, `.github/workflows/ci.yml`: redacted secret prevention.
- `pyproject.toml`, `LICENSE`, docs and tests: release hygiene and user guidance.

### Task 1: Deliver artifacts before memory effects

**Files:**
- Modify: `src/csaf/skills/types.py`
- Modify: `src/csaf/skills/runner.py`
- Modify: `src/csaf/skills/__init__.py`
- Test: `tests/skills/test_sdk.py`

- [ ] **Step 1: Write failing ordering tests**

```python
def test_artifact_handler_runs_before_memory_commit() -> None:
    delivered: list[str] = []
    with SQLiteMemoryStore() as memory:
        registry = SkillRegistry()
        registry.register(RiskDigestSkill())
        result = SkillRunner(registry, memory).run(
            "risk-digest", {"customer_id": "acme"},
            artifact_handler=lambda items: delivered.extend(x.filename for x in items),
        )
        assert delivered == ["risk-digest.md"]
        assert len(result.memory_updates) == 1


def test_artifact_handler_failure_prevents_memory_commit() -> None:
    def fail(_: tuple[Artifact, ...]) -> None:
        raise OSError("destination unavailable")
    with SQLiteMemoryStore() as memory:
        registry = SkillRegistry()
        registry.register(RiskDigestSkill())
        with pytest.raises(OSError, match="destination unavailable"):
            SkillRunner(registry, memory).run(
                "risk-digest", {"customer_id": "acme"}, artifact_handler=fail
            )
        assert memory.history("acme", "skill:risk-digest") == []
```

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/skills/test_sdk.py -k artifact_handler -v`
Expected: failure because the handler keyword does not exist.

- [ ] **Step 3: Implement the minimal contract**

```python
# skills/types.py
from collections.abc import Callable
ArtifactHandler = Callable[[tuple[Artifact, ...]], None]

# skills/runner.py signature
def run(
    self,
    name: str,
    raw_input: BaseModel | Mapping[str, Any],
    *,
    artifact_handler: ArtifactHandler | None = None,
) -> SkillRunResult[Any]:
```

Immediately after `_validate_effects(name, customer_id, draft)`, replace the
current memory-update line with:

```python
if artifact_handler is not None:
    artifact_handler(draft.artifacts)
updates = tuple(self._memory.append(update) for update in draft.memory_updates)
```

Re-export `ArtifactHandler` from `csaf.skills`; all other runner statements keep
their current order.

- [ ] **Step 4: Verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/skills/test_sdk.py -v`
Expected: all SDK tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/csaf/skills/types.py src/csaf/skills/runner.py src/csaf/skills/__init__.py tests/skills/test_sdk.py
git commit -m "fix: deliver artifacts before memory effects"
```

### Task 2: Add safe atomic CLI delivery

**Files:**
- Create: `src/csaf/cli/artifacts.py`
- Modify: `src/csaf/cli/app.py`
- Test: `tests/transports/test_cli.py`

- [ ] **Step 1: Write failing regressions**

```python
def test_account_brief_creates_output_parent(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "brief.md"
    result = runner.invoke(app, [
        "--database", str(tmp_path / "memory.db"), "account-brief", "acme",
        "--output", str(output),
    ])
    assert result.exit_code == 0
    assert output.read_text().startswith("# Account Brief: acme")


def test_output_failure_has_no_traceback_or_memory_effect(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "memory.db"
    monkeypatch.setattr("csaf.cli.artifacts.os.replace", lambda *_: (_ for _ in ()).throw(OSError("disk unavailable")))
    result = runner.invoke(app, [
        "--database", str(database), "account-brief", "acme",
        "--output", str(tmp_path / "brief.md"),
    ])
    assert result.exit_code == 2
    assert "Error: disk unavailable" in result.stderr
    assert "Traceback" not in result.output
    with SQLiteMemoryStore(database) as memory:
        assert memory.history("acme", "account-brief:last-generated") == []
```

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/transports/test_cli.py -k "output_parent or output_failure" -v`
Expected: missing parent fails and the monkeypatch target is absent.

- [ ] **Step 3: Implement staging and atomic replacement**

```python
def deliver_artifacts(artifacts: tuple[Artifact, ...], destinations: Mapping[str, Path]) -> None:
    staged: list[tuple[Path, Path]] = []
    try:
        for artifact in artifacts:
            if Path(artifact.filename).name != artifact.filename:
                raise OSError(f"unsafe artifact filename: {artifact.filename}")
            if (destination := destinations.get(artifact.filename)) is None:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(artifact.content)
                stream.flush()
                os.fsync(stream.fileno())
            staged.append((temporary, destination))
        for temporary, destination in staged:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
```

Pass a destination-bound handler into Account Brief, Meeting and QBR runner
calls. Catch `OSError` and emit exit code `2`; remove post-run `write_bytes` calls.

- [ ] **Step 4: Verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/transports/test_cli.py -v`
Expected: all CLI tests pass without tracebacks.

- [ ] **Step 5: Commit**

```powershell
git add src/csaf/cli/artifacts.py src/csaf/cli/app.py tests/transports/test_cli.py
git commit -m "fix: make CLI artifact delivery safe"
```

### Task 3: Make generic skill execution PowerShell-friendly

**Files:**
- Modify: `src/csaf/cli/app.py`
- Modify: `docs/cli.md`
- Test: `tests/transports/test_cli.py`

- [ ] **Step 1: Write failing input-file tests**

```python
def test_skill_run_accepts_input_file_and_omits_content(tmp_path: Path) -> None:
    input_file = tmp_path / "input.json"
    input_file.write_text('{"customer_id":"acme"}')
    result = runner.invoke(app, [
        "--database", str(tmp_path / "memory.db"), "skill", "run", "account-brief",
        "--input-file", str(input_file),
    ])
    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert "content" not in payload["artifacts"][0]


def test_skill_run_rejects_two_input_sources(tmp_path: Path) -> None:
    input_file = tmp_path / "input.json"
    input_file.write_text('{"customer_id":"acme"}')
    result = runner.invoke(app, [
        "skill", "run", "account-brief", "--input", '{"customer_id":"acme"}',
        "--input-file", str(input_file),
    ])
    assert result.exit_code == 2
    assert "provide exactly one of --input or --input-file" in result.stderr
```

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/transports/test_cli.py -k "input_file or input_sources" -v`
Expected: unknown-option failure and current JSON contains artifact content.

- [ ] **Step 3: Implement file input and compact output**

Make `input_json` and `input_file` optional, then select exactly one:

```python
if int(input_json is not None) + int(input_file is not None) != 1:
    raise ValueError("provide exactly one of --input or --input-file")
raw = input_json if input_json is not None else input_file.read_text(encoding="utf-8")
payload = json.loads(raw)
```

Add `--include-artifact-content` and `--output-dir`. Use the Task 2 handler for
`--output-dir`. Exclude content by default:

```python
exclude = None if include_artifact_content else {"artifacts": {"__all__": {"content"}}}
_emit(result.model_dump(mode="json", exclude=exclude))
```

Document `--input-file` as the recommended PowerShell route.

- [ ] **Step 4: Verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/transports/test_cli.py tests/test_documentation.py -v`
Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/csaf/cli/app.py docs/cli.md tests/transports/test_cli.py
git commit -m "feat: add file-based skill input"
```

### Task 4: Separate actions, commitments, and product feedback

**Files:**
- Modify: `src/csaf/schemas/memory.py`
- Modify: `src/csaf/skills/builtin/meeting_copilot.py`
- Test: `tests/skills/test_meeting_copilot.py`
- Modify: `evaluations/golden/meeting-copilot.json`

- [ ] **Step 1: Write failing taxonomy assertions**

```python
actions = [x for x in result.memory_updates if x.kind is MemoryKind.ACTION_ITEM]
commitments = [x for x in result.memory_updates if x.kind is MemoryKind.COMMITMENT]
assert [x.content for x in actions] == ["Follow up with security about API access."]
assert [x.content for x in commitments] == ["We will send the mapping document on Friday."]
assert MeetingCopilotSkill.metadata.version == "1.1.0"
```

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/skills/test_meeting_copilot.py -v`
Expected: `ACTION_ITEM` is absent and actions are stored as commitments.

- [ ] **Step 3: Implement taxonomy and normalization**

Add `ACTION_ITEM = "action_item"`, declare it as a write, bump version, and persist actions separately from commitments. Add `_without_prefix(text, prefixes)` for display text while preserving source excerpts.

- [ ] **Step 4: Update the golden file and verify GREEN**

Add `"action_item": 1` to expected writes. Run: `.\.venv\Scripts\python.exe -m pytest tests/skills/test_meeting_copilot.py tests/evaluations/test_runner.py -v`
Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/csaf/schemas/memory.py src/csaf/skills/builtin/meeting_copilot.py tests/skills/test_meeting_copilot.py evaluations/golden/meeting-copilot.json
git commit -m "fix: separate meeting actions from commitments"
```

### Task 5: Improve Account Brief output

**Files:**
- Modify: `src/csaf/skills/builtin/account_brief.py`
- Test: `tests/skills/test_account_brief.py`
- Modify: `evaluations/golden/account-brief.json`

- [ ] **Step 1: Write failing quality tests**

```python
assert result.output.action_items[0].text == "Follow up with security."
assert result.output.product_feedback[0].text == "Bulk provisioning requested."
assert result.output.opportunities == ()
assert len(result.output.recent_activity) == 1
assert "1 risk, 1 commitment, and 1 stakeholder" in result.output.executive_summary
assert AccountBriefSkill.metadata.version == "1.1.0"
```

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/skills/test_account_brief.py -v`
Expected: new fields are absent, feedback is an opportunity, and activity duplicates.

- [ ] **Step 3: Implement fields, deduplication, and grammar**

Add defaulted `action_items` and `product_feedback`, declare action-item reads, and populate:

```python
action_items=self._evidence(groups[MemoryKind.ACTION_ITEM]),
opportunities=(),
product_feedback=self._evidence(groups[MemoryKind.FEATURE_REQUEST]),
```

Deduplicate by `meeting_id` preferring timeline records, correct pluralization, add both Markdown sections, and strip duplicate category prefixes in recommendations.

- [ ] **Step 4: Verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/skills/test_account_brief.py tests/evaluations/test_runner.py -v`
Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/csaf/skills/builtin/account_brief.py tests/skills/test_account_brief.py evaluations/golden/account-brief.json
git commit -m "fix: improve deterministic account briefs"
```

### Task 6: Implement the selected local OfficeCLI adapter

**Files:**
- Modify: `src/csaf/office/officecli.py`
- Modify: `src/csaf/office/__init__.py`
- Test: `tests/office/test_officecli.py`

- [ ] **Step 1: Write failing contract tests**

Use a fake executable for version/create/batch/validate/view:

```python
config = OfficeCLIConfig(executable=sys.executable, prefix_arguments=(str(bridge),), minimum_version=(1, 0, 137))
assert [call[0] for call in calls] == ["--version", "create", "batch", "validate", "view"]
```

Add update/template copying, outdated version, validation failure, missing executable, and unchanged-source cases.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/office/test_officecli.py -v`
Expected: the selected command contract is absent.

- [ ] **Step 3: Implement create/batch/validate/view**

```python
@dataclass(frozen=True, slots=True)
class OfficeCLIConfig:
    executable: str = "officecli"
    prefix_arguments: tuple[str, ...] = ()
    timeout_seconds: float = 120.0
    minimum_version: tuple[int, int, int] = (1, 0, 137)
```

Add `_run`, `_version`, and format-specific batch builders. Use `OFFICECLI_RESIDENT_FLUSH=each`, copy templates/existing files, keep content in JSON, and validate before returning bytes.

- [ ] **Step 4: Verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/office/test_officecli.py tests/skills/test_qbr.py -v`
Expected: adapter and QBR protocol tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/csaf/office/officecli.py src/csaf/office/__init__.py tests/office/test_officecli.py
git commit -m "feat: integrate local deterministic OfficeCLI"
```

### Task 7: Add Office doctor and QBR preflight

**Files:**
- Create: `src/csaf/office/diagnostics.py`
- Modify: `src/csaf/office/__init__.py`
- Modify: `src/csaf/cli/app.py`
- Modify: `src/csaf/skills/builtin/qbr.py`
- Test: `tests/office/test_officecli.py`
- Test: `tests/transports/test_cli.py`
- Test: `tests/skills/test_qbr.py`

- [ ] **Step 1: Write failing diagnostics tests**

```python
report = OfficeCLIDoctor(config).run()
assert report.ready is True
assert [x.name for x in report.checks] == ["executable", "version", "powerpoint-smoke", "word-smoke"]
```

Add missing/outdated cases, doctor JSON CLI coverage, and QBR action-item coverage.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/office tests/transports/test_cli.py tests/skills/test_qbr.py -k "doctor or preflight or action_item" -v`
Expected: diagnostics and the Office command are absent.

- [ ] **Step 3: Implement diagnostics and preflight**

```python
class DiagnosticStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"

class DiagnosticCheck(BaseModel):
    name: str
    status: DiagnosticStatus
    message: str

class OfficeDiagnosticReport(BaseModel):
    ready: bool
    checks: tuple[DiagnosticCheck, ...]
```

Check PATH/version and smoke-render temporary PPTX/DOCX files. Add `csaf office doctor`, installation guidance without execution, QBR quick preflight, action-item reads, and skill version `1.1.0`.

- [ ] **Step 4: Verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/office tests/skills/test_qbr.py tests/transports/test_cli.py -v`
Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/csaf/office/diagnostics.py src/csaf/office/__init__.py src/csaf/cli/app.py src/csaf/skills/builtin/qbr.py tests/office tests/transports/test_cli.py tests/skills/test_qbr.py
git commit -m "feat: add OfficeCLI readiness diagnostics"
```

### Task 8: Add deterministic QBR golden evaluation

**Files:**
- Modify: `src/csaf/evaluations/runner.py`
- Create: `evaluations/golden/qbr.json`
- Test: `tests/evaluations/test_runner.py`

- [ ] **Step 1: Write a failing three-skill assertion**

```python
def test_bundled_golden_cases_include_qbr() -> None:
    cases = load_golden_cases(Path("evaluations/golden"))
    assert {case.skill_name for case in cases} == {"account-brief", "meeting-copilot", "qbr"}
    report = EvaluationRunner().run(cases)
    assert report.passed is True
    assert report.cases_total == 3
```

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/evaluations/test_runner.py -k bundled_golden -v`
Expected: only two golden skills exist.

- [ ] **Step 3: Add evaluation-only Office bytes and QBR data**

```python
class _EvaluationOfficeRenderer:
    def render(self, request: OfficeRenderRequest) -> bytes:
        return request.model_dump_json().encode("utf-8")

def _default_runtime() -> Runtime:
    return create_runtime(office_renderer=_EvaluationOfficeRenderer())
```

Use this default factory. Add `qbr.json` with goal, usage, risk, action and
commitment memory; require cited sections, three writes and two Office artifacts.

- [ ] **Step 4: Verify GREEN**

Run evaluation tests and `csaf --database :memory: evaluate evaluations/golden`.
Expected: three cases pass at `1.0`.

- [ ] **Step 5: Commit**

```powershell
git add src/csaf/evaluations/runner.py evaluations/golden/qbr.json tests/evaluations/test_runner.py
git commit -m "test: add deterministic QBR golden case"
```

### Task 9: Add redacted secret scanning

**Files:**
- Create: `scripts/check_secrets.py`
- Create: `tests/security/test_secret_scan.py`
- Modify: `.gitignore`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write failing scanner tests**

```python
findings = module.scan_text("fixture.txt", "sk-" + "A" * 32)
assert len(findings) == 1
assert findings[0].category == "provider-api-key"
assert "A" * 16 not in findings[0].render()
assert module.scan_text("docs.md", "Set API_KEY in your environment.") == ()
```

Add private-key-header and clean-repository cases.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/security/test_secret_scan.py -v`
Expected: the scanner is missing.

- [ ] **Step 3: Implement redacted scanning**

Define frozen `Finding(path, line, category, commit=None)` records. Detect
credential-shaped provider, GitHub, AWS, Google, Slack and Stripe keys and PEM
private-key headers. Support `--worktree --tracked --history` through argument
arrays. Print only category/location/commit. Exit `1` for findings, `2` for tool
failure. Ignore `.env`, `.env.*`, `*.pem`, `*.key`, `credentials*.json`; allow
`.env.example`. Set CI checkout `fetch-depth: 0` and scan before tests.

- [ ] **Step 4: Verify GREEN and scan**

Run security tests and `python scripts/check_secrets.py --worktree --tracked --history`.
Expected: tests pass and the repository has zero findings.

- [ ] **Step 5: Commit**

```powershell
git add scripts/check_secrets.py tests/security/test_secret_scan.py .gitignore .github/workflows/ci.yml
git commit -m "security: prevent repository credential leaks"
```

### Task 10: Complete packaging and documentation hygiene

**Files:**
- Create: `LICENSE`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `docs/officecli.md`
- Modify: `docs/rest-api.md`
- Modify: `docs/compatibility.md`
- Test: `tests/test_documentation.py`

- [ ] **Step 1: Write failing release tests**

Assert Apache license text, correct repository URLs, the `build` dev dependency,
local OfficeCLI/doctor documentation, and host-owned REST authentication.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_documentation.py -v`
Expected: all new assertions fail.

- [ ] **Step 3: Apply release changes**

Add canonical Apache-2.0 text and wheel inclusion, correct URLs, add
`build>=1.2,<2`, replace the deprecated test-client distribution with
`httpx2>=0.28,<1`, and update all migration/setup/security docs. Install with
`uv pip install --python .\.venv\Scripts\python.exe -e ".[dev]"` so the
untracked lock file is not changed.

- [ ] **Step 4: Verify warning-free tests and wheel**

```powershell
.\.venv\Scripts\python.exe -W error -m pytest tests/test_documentation.py tests/transports/test_api.py -v
.\.venv\Scripts\python.exe -m build
.\.venv\Scripts\python.exe -m zipfile -l dist\csaf-0.1.0.dev0-py3-none-any.whl
```

Expected: no warnings and the wheel contains the license.

- [ ] **Step 5: Commit**

```powershell
git add LICENSE pyproject.toml README.md CONTRIBUTING.md docs/officecli.md docs/rest-api.md docs/compatibility.md tests/test_documentation.py
git commit -m "docs: complete release and OfficeCLI setup"
```

### Task 11: Refresh examples and verify everything

**Files:**
- Create: `examples/data/account-brief-input.json`
- Modify: `examples/README.md`
- Modify: `docs/tutorial.md`
- Regenerate locally: `usability-review-20260810/*`

- [ ] **Step 1: Add file-input examples**

```json
{
  "customer_id": "acme",
  "time_window_days": 90
}
```

Document file input and Office doctor.

- [ ] **Step 2: Verify Python 3.12**

```powershell
.\.venv\Scripts\python.exe -W error -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\csaf.exe --database :memory: evaluate evaluations/golden --report usability-review-20260810/evaluation-report.json
```

Expected: warning-free suite, clean Ruff and three passing goldens.

- [ ] **Step 3: Verify Python 3.11**

Run: `uv run --isolated --python C:\Python311\python.exe --extra dev pytest -W error`
Expected: complete suite passes.

- [ ] **Step 4: Re-run dummy workflows**

Use a fresh review database; ingest samples, run both skills, test API
200/404/422/502 and doctor JSON, and inspect separated actions, commitments,
feedback and activity.

- [ ] **Step 5: Build and install the wheel**

Build, create a temporary review venv, install the wheel, run `skills list`, then
remove the temporary venv and smoke database.

- [ ] **Step 6: Run final security and Git checks**

```powershell
.\.venv\Scripts\python.exe scripts/check_secrets.py --worktree --tracked --history
git diff --check
git status --short --branch
```

Expected: no secrets/whitespace errors; preserve untracked `uv.lock` and do not
commit generated review artifacts without approval.

- [ ] **Step 7: Commit examples**

```powershell
git add examples/README.md examples/data/account-brief-input.json docs/tutorial.md
git commit -m "docs: refresh plug-and-play examples"
```
