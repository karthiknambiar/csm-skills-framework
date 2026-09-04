"""Keep simulation CI in the tested Python matrix with failure reports."""

import re
import shlex
from pathlib import Path


def test_simulations_run_after_unit_tests_in_both_python_jobs() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    job = workflow.split("\n  test:\n", 1)[1].split("\n  native-offline-smoke:", 1)[0]
    versions = re.search(r"python-version: \[([^\]]+)\]", job)
    assert versions is not None
    assert set(re.findall(r"3\.\d+", versions[1])) == {"3.11", "3.12"}
    assert "python-version: ${{ matrix.python-version }}" in job
    steps = re.split(r"(?m)^      - ", job)[1:]
    commands = [
        (index, shlex.split(match[1]))
        for index, step in enumerate(steps)
        if (match := re.search(r"(?:^|\n        )run: ([^\n]+)", step))
    ]
    unit = [index for index, command in commands if command == ["python", "-m", "pytest"]]
    simulation = [
        index
        for index, command in commands
        if command
        == [
            "csaf",
            "--database",
            ":memory:",
            "simulate",
            "evaluations/simulations",
            "--report-dir",
            "simulation-results",
        ]
    ]
    assert len(unit) == len(simulation) == 1
    assert unit[0] < simulation[0]
    assert not re.search(r"(?m)^        (?:if|continue-on-error):", steps[simulation[0]])
    uploads = [
        (index, step)
        for index, step in enumerate(steps)
        if re.search(r"(?m)^          path: simulation-results/?\s*$", step)
    ]
    assert len(uploads) == 1
    upload_index, upload = uploads[0]
    assert upload_index > simulation[0]
    assert "uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in upload
    assert re.search(r"(?m)^        if: always\(\)\s*$", upload)
    assert re.search(r"(?m)^          retention-days: 14\s*$", upload)
    assert "name: simulation-results-${{ matrix.python-version }}" in upload
    assert "csaf evaluate evaluations/golden --report evaluation-report.json" in job
    assert "\n  native-offline-smoke:" in workflow
