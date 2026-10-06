"""Public metadata requests use human authentication; payload bytes go to the gateway."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fl_common.errors import PlatformError
from fl_common.models.scheduler import Principal
from fl_common.models.submission import SubmissionRequest, SubmissionResponse, UploadTicket

from ..auth.api import AuthAPI
from .submissions import Submissions


def routes(auth: AuthAPI, submissions: Submissions | None) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["submission"])

    def configured() -> Submissions:
        if submissions is None:
            raise PlatformError(
                "TRANSFER_UNAVAILABLE", "Public transfer submission is not configured"
            )
        return submissions

    @router.post("/submissions", status_code=201)
    async def submit(
        body: SubmissionRequest, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> SubmissionResponse:
        return await configured().submit(body, principal)

    @router.post("/jobs/{job_id}/upload")
    async def ticket(
        job_id: UUID, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> UploadTicket:
        return await configured().ticket(job_id, principal)

    return router
