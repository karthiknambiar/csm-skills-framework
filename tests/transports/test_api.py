"""REST transport tests for health, discovery, memory, and error mapping."""

# ruff: noqa: E402 -- optional transport dependencies are checked before imports

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from csaf.api import create_app
from csaf.core import create_runtime
from csaf.schemas import MemoryKind, MemoryRecordCreate


def test_health_and_builtin_skill_discovery() -> None:
    runtime = create_runtime()
    try:
        with TestClient(create_app(runtime)) as client:
            health = client.get("/health")
            skills = client.get("/skills")

        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert skills.status_code == 200
        assert [skill["name"] for skill in skills.json()] == [
            "account-brief",
            "meeting-copilot",
            "qbr",
        ]
    finally:
        runtime.memory.close()


def test_memory_endpoint_is_customer_scoped_and_filterable() -> None:
    runtime = create_runtime()
    try:
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.RISK,
                content="Migration is delayed.",
            )
        )
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="globex",
                kind=MemoryKind.RISK,
                content="Must not leak.",
            )
        )

        with TestClient(create_app(runtime)) as client:
            response = client.get(
                "/customer/acme/memory",
                params={"kinds": "risk", "text": "migration"},
            )

        assert response.status_code == 200
        assert [record["customer_id"] for record in response.json()] == ["acme"]
    finally:
        runtime.memory.close()


def test_unknown_skill_maps_to_not_found() -> None:
    runtime = create_runtime()
    try:
        with TestClient(create_app(runtime)) as client:
            response = client.post(
                "/skills/unknown",
                json={"input": {"customer_id": "acme"}},
            )

        assert response.status_code == 404
        assert response.json() == {"detail": "skill is not registered: unknown"}
    finally:
        runtime.memory.close()


def test_account_brief_runs_through_rest_and_commits_generation_event() -> None:
    runtime = create_runtime()
    try:
        runtime.memory.append(
            MemoryRecordCreate(
                customer_id="acme",
                kind=MemoryKind.STAKEHOLDER,
                content="Jordan is the technical owner.",
            )
        )

        with TestClient(create_app(runtime)) as client:
            response = client.post(
                "/skills/account-brief",
                json={"input": {"customer_id": "acme"}},
            )

        assert response.status_code == 200
        assert response.json()["output"]["stakeholders"][0]["text"] == (
            "Jordan is the technical owner."
        )
        assert len(runtime.memory.history("acme", "account-brief:last-generated")) == 1
    finally:
        runtime.memory.close()


def test_meeting_copilot_runs_through_rest() -> None:
    runtime = create_runtime()
    try:
        with TestClient(create_app(runtime)) as client:
            response = client.post(
                "/skills/meeting-copilot",
                json={
                    "input": {
                        "customer_id": "acme",
                        "meeting_id": "meeting-1",
                        "transcript": "Alex: We will send the rollout plan.",
                    }
                },
            )

        assert response.status_code == 200
        assert response.json()["output"]["commitments"][0]["speaker"] == "Alex"
        assert runtime.memory.history("acme", "meeting:meeting-1")
    finally:
        runtime.memory.close()
