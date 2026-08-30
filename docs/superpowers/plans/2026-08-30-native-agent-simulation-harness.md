# Native-Agent Simulation Harness Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Run the same versioned customer-journey scenarios through Codex, Claude Code, and Gemini CLI in isolated workspaces, with bounded tools, reproducible evidence, and reliable infrastructure-failure classification.

**Architecture:** Add an adapter-neutral native-run contract above three CLI adapters. Each run receives a staged CSAF plugin/skill, synthetic scenario workspace, strict tool policy, time/turn/cost bounds, and redacted output directory. Native behavior is graded by deterministic policy checks first; model quality remains advisory until the separate judge-calibration phase.

**Tech Stack:** Python 3.11, Pydantic, Typer, pytest, subprocess, Codex CLI 0.151.0, Claude Code 2.1.251, Gemini CLI 0.57.0, GitHub Actions.

---

## Task 1: Add native-run contracts to the shared DSL

**Files:**
- Modify: `src/csaf/simulations/schema.py`
- Modify: `src/csaf/simulations/schema.json`
- Modify: `tests/unit/test_simulation_types.py`
- Modify: `evaluations/simulations/schema/v1.json`

**Step 1: Write failing contract tests**

Add tests proving a scenario may declare agent prompt, allowed tools, maximum turns, timeout, and expected policy events, while deterministic-only scenarios remain valid:

```python
def test_native_contract_is_optional() -> None:
    scenario = SimulationScenario.model_validate({
        "schema_version": "1.0",
        "id": "new-customer-sparse-memory",
        "title": "New customer with sparse memory",
        "seed": 11,
        "fixtures": [],
        "steps": [],
        "assertions": [],
    })
    assert scenario.native is None


def test_native_contract_rejects_unbounded_run() -> None:
    with pytest.raises(ValidationError):
        NativeScenarioSpec(prompt="Prepare the account brief", max_turns=0)
```

Run: `pytest tests/unit/test_simulation_types.py -q`
Expected: FAIL because native models do not exist.

**Step 2: Add exact models**

```python
class PolicyEvent(str, Enum):
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    OFFICE_RENDER = "office_render"
    CONSENT_REQUEST = "consent_request"
    SECRET_EXPOSURE = "secret_exposure"


class NativeScenarioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1)
    allowed_tools: list[str] = Field(default_factory=list)
    required_events: list[PolicyEvent] = Field(default_factory=list)
    forbidden_events: list[PolicyEvent] = Field(default_factory=lambda: [PolicyEvent.SECRET_EXPOSURE])
    max_turns: int = Field(default=12, ge=1, le=30)
    timeout_seconds: int = Field(default=300, ge=30, le=900)


class SimulationScenario(BaseModel):
    # existing fields stay unchanged
    native: NativeScenarioSpec | None = None
```

Regenerate both JSON schemas from the Pydantic model; assert checked-in schemas equal generated output.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_simulation_types.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/schema.py src/csaf/simulations/schema.json evaluations/simulations/schema/v1.json tests/unit/test_simulation_types.py
git commit -m "feat: define native simulation contract"
```

## Task 2: Build a bounded process boundary

**Files:**
- Create: `src/csaf/simulations/native/process.py`
- Create: `src/csaf/simulations/native/types.py`
- Create: `tests/unit/test_native_process.py`

**Step 1: Write failing timeout, output-cap, and environment tests**

Use a fake `Popen` factory. Verify timeout terminates the process tree, stdout/stderr are capped at 2 MiB each, stdin is passed without shell interpolation, and only an explicit environment allowlist survives.

```python
request = ProcessRequest(
    argv=("fake-agent", "--json"),
    stdin="user supplied $(unsafe)",
    cwd=tmp_path,
    env={"PATH": "safe", "AGENT_HOME": str(tmp_path / "home")},
    timeout_seconds=30,
)
result = runner.run(request)
assert popen_call.kwargs["shell"] is False
assert result.stdout_truncated is False
```

Run: `pytest tests/unit/test_native_process.py -q`
Expected: FAIL because the module is missing.

**Step 2: Implement immutable result types and runner**

```python
class FailureClass(str, Enum):
    NONE = "none"
    PRODUCT = "product"
    INFRASTRUCTURE = "infrastructure"
    POLICY = "policy"


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
```

Use `subprocess.Popen(..., shell=False, text=True, start_new_session=True)` and platform-specific process-tree termination. Never inherit tokens or config directories unless the adapter explicitly supplies them.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_native_process.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/native tests/unit/test_native_process.py
git commit -m "feat: add bounded native process runner"
```

## Task 3: Stage isolated agent homes and CSAF installations

**Files:**
- Create: `src/csaf/simulations/native/workspace.py`
- Create: `tests/unit/test_native_workspace.py`

**Step 1: Write failing staging tests**

Verify one temporary root contains only scenario fixtures and these agent-specific locations:

```text
run/
  workspace/
  codex-home/skills/csaf/
  claude-home/
  gemini-home/.gemini/skills/csaf/
  evidence/
```

The skill source must be `plugins/csaf/skills/csaf`; Claude receives the complete `plugins/csaf` plugin through `--plugin-dir`. Assert symlinks are not followed and `.git`, caches, and credentials are excluded.

Run: `pytest tests/unit/test_native_workspace.py -q`
Expected: FAIL.

**Step 2: Implement `NativeWorkspace`**

Expose `stage(scenario, agent)`, `environment(agent)`, and `cleanup()`. Copy with an allowlist (`SKILL.md`, `agents/`, `assets/`, `references/`, `scripts/`) and reject any resolved source escaping the repository root.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_native_workspace.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/native/workspace.py tests/unit/test_native_workspace.py
git commit -m "feat: isolate native simulation workspaces"
```

## Task 4: Implement the Codex adapter

**Files:**
- Create: `src/csaf/simulations/native/adapters/base.py`
- Create: `src/csaf/simulations/native/adapters/codex.py`
- Create: `tests/unit/test_codex_native_adapter.py`
- Create: `tests/fixtures/native/codex-stream.jsonl`

**Step 1: Write failing command and parser tests**

Assert exact command construction:

```text
codex exec --ephemeral --json --sandbox workspace-write --approve-for-me --cd <workspace> --model <model> -
```

Set `CODEX_HOME=<run>/codex-home`. Feed the checked-in JSONL stream and assert normalized assistant messages, tool calls, token usage, final result, and nonzero exit handling.

Run: `pytest tests/unit/test_codex_native_adapter.py -q`
Expected: FAIL.

**Step 2: Implement adapter and normalized events**

```python
class NativeEventType(str, Enum):
    MESSAGE = "message"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    RESULT = "result"
    ERROR = "error"
```

Unknown JSONL events are retained as sanitized `metadata`, not treated as parser errors.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_codex_native_adapter.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/native/adapters tests/unit/test_codex_native_adapter.py tests/fixtures/native/codex-stream.jsonl
git commit -m "feat: add Codex simulation adapter"
```

## Task 5: Implement Claude Code and Gemini adapters

**Files:**
- Create: `src/csaf/simulations/native/adapters/claude.py`
- Create: `src/csaf/simulations/native/adapters/gemini.py`
- Create: `tests/unit/test_claude_native_adapter.py`
- Create: `tests/unit/test_gemini_native_adapter.py`
- Create: `tests/fixtures/native/claude-stream.jsonl`
- Create: `tests/fixtures/native/gemini-stream.jsonl`
- Create: `evaluations/simulations/native-agent-versions.json`

**Step 1: Write failing command-contract tests**

Pin and assert:

```json
{
  "codex": "0.151.0",
  "claude": "2.1.251",
  "gemini": "0.57.0"
}
```

Claude command:

```text
claude -p --output-format stream-json --plugin-dir <repo>/plugins/csaf --allowedTools Read Glob Grep Bash(csaf:*)
```

Set `CLAUDE_CONFIG_DIR=<run>/claude-home`. Gemini command:

```text
gemini --output-format stream-json --sandbox --skip-trust --policy <run>/gemini-policy.toml
```

Set `GEMINI_CLI_HOME=<run>/gemini-home`. The generated Gemini policy must allow only filesystem reads inside the staged workspace and `csaf` commands; all other tools deny. Headless `ask_user` outcomes normalize to policy denial.

Run: `pytest tests/unit/test_claude_native_adapter.py tests/unit/test_gemini_native_adapter.py -q`
Expected: FAIL.

**Step 2: Implement parsers and exit classification**

Normalize both JSONL formats to the shared event model. Gemini exits 1/42/53 and Claude malformed/empty streams are infrastructure failures unless the stream contains an explicit product or policy failure.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_claude_native_adapter.py tests/unit/test_gemini_native_adapter.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/native/adapters tests/unit/test_claude_native_adapter.py tests/unit/test_gemini_native_adapter.py tests/fixtures/native evaluations/simulations/native-agent-versions.json
git commit -m "feat: add Claude and Gemini adapters"
```

## Task 6: Add native policy grading

**Files:**
- Create: `src/csaf/simulations/native/policy.py`
- Create: `tests/unit/test_native_policy.py`

**Step 1: Write failing hard-gate tests**

Cover required and forbidden events, workspace escape, unapproved OfficeCLI install, uncited QBR template download, cross-customer memory access, secrets in output, and a consent-denied-then-approved recovery.

```python
grade = grader.grade(scenario, events)
assert grade.passed is False
assert grade.failures[0].code == "officecli_install_without_consent"
```

Run: `pytest tests/unit/test_native_policy.py -q`
Expected: FAIL.

**Step 2: Implement deterministic evidence extraction**

Match structured tool arguments and results, never free-form chain-of-thought. Apply `redact_officecli_message` plus generic key/token redaction before evidence persistence. Unknown tool calls fail closed.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_native_policy.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/native/policy.py tests/unit/test_native_policy.py
git commit -m "feat: grade native simulation policy"
```

## Task 7: Orchestrate runs, retries, and evidence

**Files:**
- Create: `src/csaf/simulations/native/runner.py`
- Create: `src/csaf/simulations/native/report.py`
- Create: `tests/unit/test_native_runner.py`
- Create: `tests/integration/test_native_simulation_fake_agents.py`

**Step 1: Write failing orchestration tests**

Test fixed scenario order, unique run IDs, timeout/turn limits, one infrastructure retry, zero product/policy retries, and sanitized JSON/Markdown reports. Fake executables emit the three checked-in agent streams, so integration tests need no network or credentials.

Run: `pytest tests/unit/test_native_runner.py tests/integration/test_native_simulation_fake_agents.py -q`
Expected: FAIL.

**Step 2: Implement runner**

```python
for attempt in range(2):
    result = adapter.run(request)
    if result.failure_class is not FailureClass.INFRASTRUCTURE:
        break
```

Record both attempts, but grade only the final attempt. Report agent/version/model, scenario/seed, durations, token counts, tool evidence, hard-gate results, and redaction count.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_native_runner.py tests/integration/test_native_simulation_fake_agents.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/native/runner.py src/csaf/simulations/native/report.py tests/unit/test_native_runner.py tests/integration/test_native_simulation_fake_agents.py
git commit -m "feat: orchestrate native simulations"
```

## Task 8: Expose the native CLI and three-agent smoke matrix

**Files:**
- Modify: `src/csaf/cli/app.py`
- Create: `tests/cli/test_simulate_native.py`
- Modify: `evaluations/simulations/scenarios/new-customer-sparse-memory.json`
- Modify: `evaluations/simulations/scenarios/conflicting-commitments.json`
- Modify: `evaluations/simulations/scenarios/officecli-consent-recovery.json`
- Create: `.github/workflows/native-simulation-smoke.yml`
- Modify: `README.md`

**Step 1: Write failing CLI tests**

```text
csaf simulate-native --agent codex --scenario new-customer-sparse-memory --model test --output artifacts/native
csaf simulate-native --agent all --smoke --output artifacts/native
```

Verify missing CLI, missing credentials, or unavailable network yields infrastructure status and exit 2; hard-gate failure exits 1; pass exits 0.

Run: `pytest tests/cli/test_simulate_native.py -q`
Expected: FAIL.

**Step 2: Add native blocks to exactly three smoke scenarios**

Use sparse-memory, conflicting-commitments, and OfficeCLI consent recovery. Each prompt names the customer and desired artifact but does not prescribe the skill steps. Define allowed tools and required/forbidden policy events explicitly.

**Step 3: Implement CLI and workflow**

Workflow runs on internal pull requests only, installs pinned CLIs, executes 3 scenarios × 3 agents, uploads sanitized evidence, and treats infrastructure status as neutral with a visible check annotation. It must never run forked PR code with repository secrets.

**Step 4: Run tests and commit**

Run: `pytest tests/cli/test_simulate_native.py tests/integration/test_native_simulation_fake_agents.py -q`
Expected: PASS.

```bash
git add src/csaf/cli/app.py tests/cli/test_simulate_native.py evaluations/simulations/scenarios .github/workflows/native-simulation-smoke.yml README.md
git commit -m "feat: add native simulation smoke suite"
```

## Task 9: Verify the native harness

**Files:**
- Modify only if verification exposes a defect in files listed above.

**Step 1: Run focused tests**

Run: `pytest tests/unit/test_native_process.py tests/unit/test_native_workspace.py tests/unit/test_codex_native_adapter.py tests/unit/test_claude_native_adapter.py tests/unit/test_gemini_native_adapter.py tests/unit/test_native_policy.py tests/unit/test_native_runner.py tests/integration/test_native_simulation_fake_agents.py tests/cli/test_simulate_native.py -q`
Expected: PASS.

**Step 2: Run static checks and full suite**

Run: `ruff check .`
Expected: PASS.

Run: `pytest -W error -q`
Expected: PASS with existing platform-dependent skips only.

**Step 3: Exercise installed CLIs when credentials exist**

Run: `csaf simulate-native --agent all --smoke --output artifacts/native-manual`
Expected: each configured agent passes, or returns explicit infrastructure status without modifying product state.

**Step 4: Commit verification fixes, if any**

If verification changed a listed file, stage that exact file and commit it as `fix: harden native simulation harness`; otherwise create no commit.
