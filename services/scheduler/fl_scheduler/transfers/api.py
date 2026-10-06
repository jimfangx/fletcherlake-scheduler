"""An owner may request delivery; only the private gateway can attest the payload."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fl_common.errors import PlatformError
from fl_common.models.base import Schema
from fl_common.models.scheduler import Principal

from ..auth.api import AuthAPI
from .service import TransferService


class DeliveryRequest(Schema):
    upload_id: UUID


def routes(auth: AuthAPI, transfers: TransferService | None) -> APIRouter:
    router = APIRouter(prefix="/api/jobs", tags=["transfer"])

    @router.post("/{job_id}/delivery", status_code=202)
    async def begin(
        job_id: UUID,
        body: DeliveryRequest,
        principal: Annotated[Principal, Depends(auth.principal)],
    ) -> dict[str, str]:
        if transfers is None:
            raise PlatformError("TRANSFER_UNAVAILABLE", "Transfer gateway is not configured")
        transfer_id = await transfers.begin(job_id, body.upload_id, principal)
        return {"job_id": str(job_id), "transfer_id": str(transfer_id)}

    return router
