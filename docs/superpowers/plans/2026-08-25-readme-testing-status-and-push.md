# README Testing Status and GitHub Push Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the completed native-agent work with a prominent README notice that the integration is still being tested.

**Architecture:** Add one exact documentation contract and one README blockquote without changing installer behavior. Verify the complete rewritten branch, update local `main` only after comparing it with `origin/main`, then push `main` without force so the public repository README reflects the testing status.

**Tech Stack:** Markdown, pytest, Typer documentation contracts, Ruff, Git, GitHub.

---

### Task 1: Add the testing-status notice

**Files:**
- Modify: `README.md`
- Modify: `tests/test_documentation.py`

- [ ] **Step 1: Write the failing documentation contract**

Add a test that requires the exact approved notice and verifies its position after the opening value statement and before the OfficeCLI and installation sections:

```python
def test_readme_prominently_marks_native_integration_as_testing() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    notice = (
        "> **Testing status:** CSAF’s native agent integration is still being tested. "
        "Use non-production data, review each setup plan before consenting, and report "
        "unexpected behavior."
    )

    assert notice in readme
    assert readme.index("CSAF turns local customer context") < readme.index(notice)
    assert readme.index(notice) < readme.index("OfficeCLI is mandatory")
    assert readme.index(notice) < readme.index("## Install")
```

- [ ] **Step 2: Run the focused test to verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -W error -m pytest tests/test_documentation.py::test_readme_prominently_marks_native_integration_as_testing -v
```

Expected: FAIL because the approved notice is absent.

- [ ] **Step 3: Add the approved README notice**

Insert this blockquote immediately after the opening value statement:

```markdown
> **Testing status:** CSAF’s native agent integration is still being tested. Use non-production data, review each setup plan before consenting, and report unexpected behavior.
```

- [ ] **Step 4: Run GREEN and documentation gates**

Run:

```powershell
.\.venv\Scripts\python.exe -W error -m pytest tests/test_documentation.py -v
```

Expected: all documentation tests pass, all local links resolve, installer commands remain tagged, and README remains within 180 nonblank lines.

- [ ] **Step 5: Commit the notice and regression**

```powershell
git add -- README.md tests/test_documentation.py
git commit -m "docs: mark native integration as testing"
```

### Task 2: Verify and publish `main`

**Files:**
- Modify only Git refs after verification; do not modify source files.

- [ ] **Step 1: Run final local gates**

Run:

```powershell
.\.venv\Scripts\python.exe -W error -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts
.\.venv\Scripts\python.exe scripts/check_secrets.py --worktree --tracked --history
git diff --check
git status --short
```

Expected: all tests and checks pass and the feature worktree is clean.

- [ ] **Step 2: Inspect GitHub state without changing it**

Fetch `origin/main`, verify authentication, confirm the repository URL, and compare `origin/main`, local `main`, and `codex/native-agent-installation`. Stop on unexpected divergence; never force-push.

- [ ] **Step 3: Fast-forward local `main`**

Because `main` is expected to be an ancestor of the feature branch, update the main worktree with:

```powershell
git merge --ff-only codex/native-agent-installation
```

Expected: local `main` points to the fully verified feature HEAD without a merge commit.

- [ ] **Step 4: Verify the integrated main worktree**

Run the full warning-as-error suite and combined history scan from the main worktree. Confirm the public README notice is present and the worktree is clean.

- [ ] **Step 5: Push without force**

```powershell
git push origin main
```

Expected: GitHub accepts the fast-forward update. Do not create a stable tag or GitHub release; current-commit cross-platform CI remains a prerequisite for tagging.
