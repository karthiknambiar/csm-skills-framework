"""Deterministic runtime dependency tests."""

import subprocess
import sys
from datetime import UTC, datetime
from uuid import UUID

from csaf.core import create_runtime


def test_memory_store_imports_in_a_fresh_process() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from csaf.memory import SQLiteMemoryStore; "
                "from csaf.core import Runtime, create_runtime"
            ),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_runtime_uses_injected_time_and_ids() -> None:
    fixed_now = datetime(2026, 7, 1, 9, tzinfo=UTC)
    execution_id = UUID("00000000-0000-0000-0000-000000000001")
    memory_update_id = UUID("00000000-0000-0000-0000-000000000002")
    ids = iter((execution_id, memory_update_id))
    runtime = create_runtime(now=lambda: fixed_now, id_factory=ids.__next__)

    result = runtime.runner.run("account-brief", {"customer_id": "customer-1"})

    assert result.execution_id == execution_id
    assert result.started_at == fixed_now
    assert result.completed_at == fixed_now
    assert result.output.generated_at == fixed_now
    assert result.memory_updates[0].id == memory_update_id
    assert result.memory_updates[0].created_at == fixed_now
