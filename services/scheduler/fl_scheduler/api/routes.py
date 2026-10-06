"""Authenticated public metadata and job/control endpoints."""

import asyncio
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fl_common.models import ArtifactRef, JobConfig, JobSpec
from fl_common.models.base import Schema
from fl_common.models.scheduler import Principal

from ..auth.api import AuthAPI
from ..scheduler.placement import Placement
from .control import Control
from .queries import Queries


class Submission(Schema):
    config: JobConfig
    binary: ArtifactRef | None = None
    bitstream: ArtifactRef | None = None


def routes(auth: AuthAPI, queries: Queries, placement: Placement, control: Control) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["scheduler"])

    @router.post("/jobs", status_code=201)
    async def submit(
        body: Submission, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> JobSpec:
        return await asyncio.to_thread(
            placement.submit, body.config, principal, binary=body.binary, bitstream=body.bitstream
        )

    @router.get("/jobs")
    async def jobs(
        principal: Annotated[Principal, Depends(auth.principal)],
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(queries.jobs, principal, limit, offset)

    @router.get("/jobs/{job_id}")
    async def job(
        job_id: UUID, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> dict[str, Any]:
        return await asyncio.to_thread(queries.job, job_id, principal)

    @router.get("/jobs/{job_id}/events")
    async def events(
        job_id: UUID,
        principal: Annotated[Principal, Depends(auth.principal)],
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(queries.events, job_id, principal, after, limit)

    @router.get("/jobs/{job_id}/artifacts")
    async def artifacts(
        job_id: UUID, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(queries.artifacts, job_id, principal)

    @router.post("/jobs/{job_id}/cancel", status_code=202)
    async def cancel(
        job_id: UUID, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> dict[str, str]:
        await asyncio.to_thread(control.cancel, job_id, principal)
        return {"job_id": str(job_id), "status": "cancellation_requested"}

    @router.delete("/jobs/{job_id}/artifacts", status_code=202)
    async def delete(
        job_id: UUID, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> dict[str, str]:
        message_id = await asyncio.to_thread(control.delete_artifacts, job_id, principal)
        return {"command_id": str(message_id)}

    @router.get("/clusters")
    async def clusters(
        principal: Annotated[Principal, Depends(auth.principal)],
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(queries.clusters, principal)

    @router.get("/clusters/{cluster_id}")
    async def cluster(
        cluster_id: UUID, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> dict[str, Any]:
        return await asyncio.to_thread(queries.cluster, cluster_id, principal)

    @router.get("/boards/{board_id}")
    async def board(
        board_id: str, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> dict[str, Any]:
        return await asyncio.to_thread(queries.board, board_id, principal)

    @router.post("/clusters/{cluster_id}/drain", status_code=202)
    async def drain(
        cluster_id: UUID, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> dict[str, str]:
        return {"command_id": str(await asyncio.to_thread(control.drain, cluster_id, principal))}

    @router.get("/admin/users")
    async def users(
        principal: Annotated[Principal, Depends(auth.principal)],
    ) -> list[dict[str, str]]:
        return await asyncio.to_thread(queries.users, principal)

    return router
