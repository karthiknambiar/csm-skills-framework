"""FastAPI application factory for CSAF transport operations."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from csaf import __version__
from csaf.core import Runtime, create_runtime
from csaf.schemas import MemoryKind, MemoryQuery, MemoryRecord
from csaf.skills.errors import SkillContractError, SkillExecutionError, SkillNotFoundError
from csaf.skills.types import SkillMetadata


class HealthResponse(BaseModel):
    """Small readiness response for deployment probes."""

    status: str
    version: str


class SkillInvocation(BaseModel):
    """Transport wrapper around a skill's own structured input."""

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """Create an isolated API application around an injected or local runtime."""

    owns_runtime = runtime is None
    active_runtime = runtime or create_runtime()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_runtime:
            active_runtime.memory.close()

    app = FastAPI(
        title="Customer Success Agent Framework",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.runtime = active_runtime

    @app.exception_handler(SkillNotFoundError)
    async def skill_not_found(_: Request, error: SkillNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(SkillContractError)
    async def skill_contract_error(_: Request, error: SkillContractError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @app.exception_handler(SkillExecutionError)
    async def skill_execution_error(_: Request, error: SkillExecutionError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(error)})

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    @app.get("/skills", response_model=list[SkillMetadata], tags=["skills"])
    def list_skills() -> list[SkillMetadata]:
        return [skill.metadata for skill in active_runtime.skills]

    @app.post("/skills/{skill_name}", tags=["skills"])
    def run_skill(skill_name: str, invocation: SkillInvocation) -> dict[str, Any]:
        result = active_runtime.runner.run(skill_name, invocation.input)
        return result.model_dump(mode="json")

    @app.get(
        "/customer/{customer_id}/memory",
        response_model=list[MemoryRecord],
        tags=["memory"],
    )
    def inspect_memory(
        customer_id: str,
        kinds: Annotated[list[MemoryKind] | None, Query()] = None,
        text: str | None = None,
        latest_only: bool = False,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> list[MemoryRecord]:
        return active_runtime.memory.search(
            MemoryQuery(
                customer_id=customer_id,
                kinds=tuple(kinds or ()),
                text=text,
                latest_only=latest_only,
                limit=limit,
            )
        )

    return app
