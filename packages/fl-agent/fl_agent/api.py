"""Private local API. Production transport is a permission-restricted Unix socket."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fl_common.errors import PlatformError
from fl_common.models import ClusterState, JobConfig
from fl_common.models.artifact import ArtifactKind
from pydantic import BaseModel

from .service import AgentService


class DrainRequest(BaseModel):
    state: Literal[
        ClusterState.DRAINING,
        ClusterState.RECONFIGURING,
        ClusterState.RESTARTING,
        ClusterState.DESTROYED,
    ] = ClusterState.DRAINING


def create_app(service: AgentService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="fl-agent local API", lifespan=lifespan)

    @app.exception_handler(PlatformError)
    async def platform_error(request: Request, error: PlatformError) -> JSONResponse:
        status = 404 if error.code == "JOB_NOT_FOUND" else 409
        return JSONResponse(status_code=status, content=error.as_dict())

    @app.get("/v1/status")
    async def status() -> dict[str, object]:
        return service.snapshot()

    @app.get("/v1/jobs")
    async def jobs() -> list[dict[str, object]]:
        return [job.model_dump(mode="json") for job in service.db.jobs()]

    @app.post("/v1/jobs", status_code=201)
    async def submit(config: JobConfig) -> dict[str, object]:
        return (await service.submit(config)).model_dump(mode="json")

    @app.get("/v1/jobs/{job_id}")
    async def job(job_id: UUID) -> dict[str, object]:
        return service.db.get(job_id).model_dump(mode="json")

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel(job_id: UUID) -> dict[str, object]:
        return service.cancel(job_id).model_dump(mode="json")

    @app.get("/v1/jobs/{job_id}/artifacts/{kind}")
    async def artifact(job_id: UUID, kind: ArtifactKind) -> FileResponse:
        service.db.get(job_id)
        path = service.store.path(job_id, kind)
        if not path.is_file():
            raise PlatformError("JOB_NOT_FOUND", "Artifact is absent or expired")
        return FileResponse(path)

    @app.delete("/v1/jobs/{job_id}/artifacts", status_code=204)
    async def delete_artifacts(job_id: UUID) -> None:
        service.store.request_deletion(job_id)
        await service.exports.cancel(job_id)
        service.store.sweep()

    @app.get("/v1/events")
    async def events(after: int = Query(default=0, ge=0)) -> list[dict[str, object]]:
        return [event.model_dump(mode="json") for event in service.db.events(after)]

    @app.post("/v1/cluster/drain")
    async def drain(request: DrainRequest) -> dict[str, str]:
        await service.drain(request.state)
        return {"state": service.state}

    return app
