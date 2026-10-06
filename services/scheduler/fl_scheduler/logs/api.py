"""Authenticated bounded log pages; this API never reads caller-selected paths."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fl_common.models.logs import MAX_LOG_BYTES, MAX_OFFSET, LogPage, LogStream
from fl_common.models.scheduler import Principal

from ..auth.api import AuthAPI
from .reads import LogReads


def routes(auth: AuthAPI, reads: LogReads) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["logs"])

    @router.get("/jobs/{job_id}/logs")
    async def logs(
        job_id: UUID,
        principal: Annotated[Principal, Depends(auth.principal)],
        stream: LogStream = "stdout",
        offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
        limit: int = Query(default=MAX_LOG_BYTES, ge=1, le=MAX_LOG_BYTES),
    ) -> LogPage:
        return await asyncio.to_thread(reads.page, job_id, principal, stream, offset, limit)

    return router
