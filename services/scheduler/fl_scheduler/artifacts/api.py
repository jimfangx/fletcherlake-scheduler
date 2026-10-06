"""Human-authenticated artifact requests carry no bytes or private identities."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fl_common.errors import PlatformError
from fl_common.models.download import DownloadRequest, DownloadTicket
from fl_common.models.scheduler import Principal
from fl_common.models.transfer import TransferEndpoint

from ..auth.api import AuthAPI
from ..db.async_calls import database_call
from .requests import Requests
from .service import ExportService
from .tickets import Tickets


class Downloads:
    def __init__(self, exports: ExportService, endpoint: TransferEndpoint) -> None:
        self.exports = exports
        self.requests = Requests(exports.state.db)
        self.tickets = Tickets(exports.state.db, endpoint)

    async def request(
        self, job_id: UUID, body: DownloadRequest, principal: Principal
    ) -> DownloadTicket:
        download_id = await database_call(self.requests.begin, job_id, body, principal)
        ticket = await database_call(self.tickets.ticket, job_id, download_id, principal)
        if ticket.grant:
            await self.exports.gateway.register(ticket.grant)
            try:
                await database_call(self.tickets.recheck, job_id, download_id, principal)
            except Exception:
                await self.exports.gateway.revoke(download_id)
                raise
        return ticket


def routes(auth: AuthAPI, downloads: Downloads | None) -> APIRouter:
    router = APIRouter(prefix="/api/jobs", tags=["artifacts"])

    @router.post("/{job_id}/downloads")
    async def download(
        job_id: UUID,
        body: DownloadRequest,
        principal: Annotated[Principal, Depends(auth.principal)],
    ) -> DownloadTicket:
        if downloads is None:
            raise PlatformError("TRANSFER_UNAVAILABLE", "Artifact exports are not configured")
        return await downloads.request(job_id, body, principal)

    return router
