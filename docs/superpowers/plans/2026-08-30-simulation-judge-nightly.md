# Simulation Judge and Nightly Matrix Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add calibrated, advisory quality scoring and a nightly 18-scenario × 3-agent matrix with trend reports, while preserving deterministic hard gates as the only initial merge blocker.

**Architecture:** A rubric judge consumes redacted native evidence and emits schema-constrained scores. Human labels calibrate agreement and bias before any blocking use. Nightly orchestration reuses the native harness, retries infrastructure failures once, compares against checked-in baselines, and publishes machine-readable plus concise Markdown reports.

**Tech Stack:** Python 3.11, Pydantic, Typer, pytest, scipy-free deterministic statistics, GitHub Actions, JSON/Markdown artifacts.

---

## Task 1: Define the versioned quality rubric and result schema

**Files:**
- Create: `src/csaf/simulations/judge/types.py`
- Create: `evaluations/simulations/rubrics/v1.json`
- Create: `tests/unit/test_judge_types.py`

**Step 1: Write failing schema tests**

Assert five 0–4 dimensions, cited evidence IDs, total score, confidence, and no unstructured rationale:

```python
result = JudgeResult.model_validate(payload)
assert result.total == sum(score.value for score in result.scores)
assert all(score.evidence_ids for score in result.scores)
```

Dimensions are `task_completion`, `evidence_grounding`, `customer_judgment`, `clarity`, and `recovery_quality`. Each rubric level contains observable criteria; no criterion asks for hidden reasoning.

Run: `pytest tests/unit/test_judge_types.py -q`
Expected: FAIL.

**Step 2: Implement strict models and checked-in rubric**

```python
class DimensionScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: RubricDimension
    value: int = Field(ge=0, le=4)
    evidence_ids: list[str] = Field(min_length=1)


class JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rubric_version: Literal["1.0"]
    scores: list[DimensionScore] = Field(min_length=5, max_length=5)
    total: int = Field(ge=0, le=20)
    confidence: float = Field(ge=0, le=1)
```

Validate uniqueness and exact dimension coverage in a model validator.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_judge_types.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/judge/types.py evaluations/simulations/rubrics/v1.json tests/unit/test_judge_types.py
git commit -m "feat: define simulation quality rubric"
```

## Task 2: Implement a schema-constrained judge adapter

**Files:**
- Create: `src/csaf/simulations/judge/adapter.py`
- Create: `src/csaf/simulations/judge/prompt.py`
- Create: `tests/unit/test_judge_adapter.py`
- Create: `tests/fixtures/judge/valid-result.json`

**Step 1: Write failing adapter tests**

Use an injected fake command runner. Assert the prompt contains only scenario requirements, rubric, redacted final answer, and stable evidence IDs. Assert malformed output, missing evidence, timeout, and leaked secret all produce `judge_infrastructure_failure`, never a product failure.

Run: `pytest tests/unit/test_judge_adapter.py -q`
Expected: FAIL.

**Step 2: Implement adapter**

The default command uses Codex `exec --ephemeral --json --output-schema <schema>`, but command and model are injectable. Set temperature-equivalent controls to the provider's deterministic setting when supported. Store rubric version, judge model, CLI version, prompt SHA-256, and parsed result.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_judge_adapter.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/judge tests/unit/test_judge_adapter.py tests/fixtures/judge/valid-result.json
git commit -m "feat: add advisory rubric judge"
```

## Task 3: Define human-label and calibration contracts

**Files:**
- Create: `src/csaf/simulations/judge/calibration.py`
- Create: `evaluations/simulations/calibration/schema.json`
- Create: `evaluations/simulations/calibration/labels.jsonl`
- Create: `tests/unit/test_judge_calibration.py`

**Step 1: Write failing calibration tests**

Require 50 distinct outputs, two independent reviewers per output, adjudication for dimension disagreement greater than one point, all three source agents represented, and at least 10 hard-gate failures.

```python
report = calibrate(labels, judge_results)
assert report.eligible_for_blocking is False
assert "fewer_than_50_outputs" in report.reasons
```

Run: `pytest tests/unit/test_judge_calibration.py -q`
Expected: FAIL.

**Step 2: Implement metrics and thresholds**

Calculate exact-score agreement, within-one agreement, hard-failure false-negative rate, and per-agent mean absolute error. Blocking eligibility requires all of:

- 50 accepted outputs;
- exact dimension agreement ≥ 80%;
- within-one agreement ≥ 95%;
- hard-failure false-negative rate = 0%;
- per-agent mean absolute error ≤ 0.50;
- no source-agent pair differs in mean absolute error by more than 0.25.

The initial `labels.jsonl` is an empty valid file. Do not fabricate human labels.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_judge_calibration.py -q`
Expected: PASS and fixture calibration reports ineligible.

```bash
git add src/csaf/simulations/judge/calibration.py evaluations/simulations/calibration tests/unit/test_judge_calibration.py
git commit -m "feat: calibrate simulation judge"
```

## Task 4: Add blind review packet and import commands

**Files:**
- Create: `src/csaf/simulations/judge/review.py`
- Modify: `src/csaf/cli/app.py`
- Create: `tests/cli/test_judge_review.py`
- Create: `docs/simulation-judge-calibration.md`

**Step 1: Write failing CLI tests**

```text
csaf judge-review export artifacts/native --sample-size 50 --seed 20260830 --output artifacts/review
csaf judge-review import artifacts/review/reviewer-a.jsonl artifacts/review/reviewer-b.jsonl --output evaluations/simulations/calibration/labels.jsonl
csaf judge-review calibrate --labels evaluations/simulations/calibration/labels.jsonl --judge-results artifacts/judge --output artifacts/calibration.json
```

Assert export removes agent identity and judge scores, uses balanced stratified sampling, and emits stable randomized IDs. Import rejects self-review, duplicate reviewer identity, invalid evidence references, and incomplete sets.

Run: `pytest tests/cli/test_judge_review.py -q`
Expected: FAIL.

**Step 2: Implement commands and operator guide**

Document reviewer independence, adjudication, PII review, and the exact eligibility thresholds. The import command writes only validated labels and preserves source evidence hashes.

**Step 3: Run tests and commit**

Run: `pytest tests/cli/test_judge_review.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/judge/review.py src/csaf/cli/app.py tests/cli/test_judge_review.py docs/simulation-judge-calibration.md
git commit -m "feat: add blind judge calibration workflow"
```

## Task 5: Add baseline and trend comparison

**Files:**
- Create: `src/csaf/simulations/trends.py`
- Create: `evaluations/simulations/baselines/v1.json`
- Create: `tests/unit/test_simulation_trends.py`

**Step 1: Write failing trend tests**

Cover new failures, recovered failures, pass-rate delta, dimension-score delta, token/latency delta, missing cells, and baseline version mismatch. A missing/infrastructure cell is reported separately and never scored as product regression.

Run: `pytest tests/unit/test_simulation_trends.py -q`
Expected: FAIL.

**Step 2: Implement comparator**

Baseline keys are `(scenario_id, agent, model, rubric_version)`. Initial baseline contains schema/version metadata and no invented measurements. Add `csaf simulation-baseline accept <nightly-report>`; it validates a complete 54-cell matrix and refuses any hard-gate failure unless `--allow-known-failure <scenario:agent>` is supplied for every exception.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_simulation_trends.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/trends.py evaluations/simulations/baselines/v1.json tests/unit/test_simulation_trends.py src/csaf/cli/app.py
git commit -m "feat: compare simulation trends"
```

## Task 6: Run the 18 × 3 nightly matrix

**Files:**
- Create: `src/csaf/simulations/matrix.py`
- Create: `tests/unit/test_simulation_matrix.py`
- Create: `.github/workflows/native-simulation-nightly.yml`

**Step 1: Write failing matrix tests**

Assert 18 scenarios × 3 agents = 54 cells, stable ordering, bounded concurrency, per-cell timeout, one infrastructure retry, completion despite individual failures, and final status precedence `policy/product failure > incomplete infrastructure > pass`.

Run: `pytest tests/unit/test_simulation_matrix.py -q`
Expected: FAIL.

**Step 2: Implement matrix builder**

```python
cells = [
    MatrixCell(scenario=scenario.id, agent=agent)
    for scenario in sorted(scenarios, key=lambda item: item.id)
    for agent in (AgentKind.CODEX, AgentKind.CLAUDE, AgentKind.GEMINI)
]
assert len(cells) == 54
```

The workflow runs nightly and by manual dispatch, uses pinned CLI versions, maximum 3 concurrent cells, 90-minute job timeout, least-privilege read-only repository permissions, no execution on fork events, and sanitized artifact retention for 30 days.

**Step 3: Keep the judge advisory**

Run judge scoring for product-success cells. Publish its failures and scores but do not alter workflow exit status. Deterministic hard gates remain blocking.

**Step 4: Run tests and commit**

Run: `pytest tests/unit/test_simulation_matrix.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/matrix.py tests/unit/test_simulation_matrix.py .github/workflows/native-simulation-nightly.yml
git commit -m "ci: add nightly native simulation matrix"
```

## Task 7: Generate unified JSON and Markdown reports

**Files:**
- Create: `src/csaf/simulations/nightly_report.py`
- Create: `tests/unit/test_nightly_report.py`
- Modify: `README.md`

**Step 1: Write failing snapshot tests**

The JSON report must contain schema version, commit SHA, timestamp, versions, 54 cells, retries, hard gates, advisory judge scores, trends, and redaction counts. Markdown must lead with actionable regressions and separate infrastructure gaps.

Run: `pytest tests/unit/test_nightly_report.py -q`
Expected: FAIL.

**Step 2: Implement reporters and README usage**

Document local deterministic simulation, optional native smoke, nightly artifact interpretation, calibration status, and explicit warning that judge scores are advisory. Do not expose prompts containing synthetic secrets.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_nightly_report.py -q`
Expected: PASS.

```bash
git add src/csaf/simulations/nightly_report.py tests/unit/test_nightly_report.py README.md
git commit -m "docs: report nightly simulation results"
```

## Task 8: Add an explicit, reviewed judge-promotion gate

**Files:**
- Create: `evaluations/simulations/judge-policy.json`
- Create: `src/csaf/simulations/judge/policy.py`
- Create: `tests/unit/test_judge_policy.py`
- Modify: `.github/workflows/native-simulation-nightly.yml`

**Step 1: Write failing policy tests**

Initial policy:

```json
{
  "schema_version": "1.0",
  "mode": "advisory",
  "rubric_version": "1.0",
  "calibration_report_sha256": null
}
```

Assert `mode=blocking` is rejected unless the calibration report is present, hash-matched, eligible, and based on at least 50 accepted outputs. Runtime flags cannot override checked-in advisory mode.

Run: `pytest tests/unit/test_judge_policy.py -q`
Expected: FAIL.

**Step 2: Implement policy validator**

Promotion requires a separate reviewed commit changing `mode` and adding the calibration hash. This phase does not promote it; it only makes future promotion safe.

**Step 3: Run tests and commit**

Run: `pytest tests/unit/test_judge_policy.py -q`
Expected: PASS with mode advisory.

```bash
git add evaluations/simulations/judge-policy.json src/csaf/simulations/judge/policy.py tests/unit/test_judge_policy.py .github/workflows/native-simulation-nightly.yml
git commit -m "feat: gate judge promotion on calibration"
```

## Task 9: Verify judge and nightly evaluation

**Files:**
- Modify only if verification exposes a defect in files listed above.

**Step 1: Run focused tests**

Run: `pytest tests/unit/test_judge_types.py tests/unit/test_judge_adapter.py tests/unit/test_judge_calibration.py tests/cli/test_judge_review.py tests/unit/test_simulation_trends.py tests/unit/test_simulation_matrix.py tests/unit/test_nightly_report.py tests/unit/test_judge_policy.py -q`
Expected: PASS.

**Step 2: Verify advisory policy**

Run: `csaf judge-review calibrate --labels evaluations/simulations/calibration/labels.jsonl --judge-results tests/fixtures/judge --output artifacts/calibration.json`
Expected: command succeeds, reports `eligible_for_blocking: false`, and judge policy remains advisory.

**Step 3: Run static checks and full suite**

Run: `ruff check .`
Expected: PASS.

Run: `pytest -W error -q`
Expected: PASS with existing platform-dependent skips only.

**Step 4: Commit verification fixes, if any**

If verification changed a listed file, stage that exact file and commit it as `fix: harden simulation evaluation`; otherwise create no commit.



