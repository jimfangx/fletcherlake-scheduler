"""Public submission IDs bind job metadata and an upload identity in one transaction."""

import hashlib
import json
from uuid import UUID

from fl_common.models import JobState
from fl_common.models.scheduler import Principal
from fl_common.models.submission import SubmissionRequest, SubmissionResponse, UploadTicket
from fl_common.models.transfer import TransferEndpoint
from fl_common.network import https_origin

from ..db.async_calls import database_call
from ..db.core import Database
from ..db.models import Job
from ..scheduler.creation import JobCreation
from ..scheduler.placement import Placement
from .service import TransferService
from .uploads import Uploads


class Submissions:
    def __init__(
        self,
        db: Database,
        transfers: TransferService,
        endpoint: TransferEndpoint,
        origin: str,
    ) -> None:
        self.db, self.transfers, self.placement = db, transfers, Placement(db)
        self.creation = JobCreation(db)
        self.uploads = Uploads(db, endpoint, https_origin(origin))

    async def submit(self, body: SubmissionRequest, principal: Principal) -> SubmissionResponse:
        encoded = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        spec = await database_call(
            self.creation.submit,
            body.config,
            principal,
            binary=body.binary,
            bitstream=body.bitstream,
            request_id=body.request_id,
            request_hash=hashlib.sha256(encoded.encode()).hexdigest(),
            public_key=body.public_key,
            token_hash=body.token_hash,
        )

        def state() -> JobState:
            with self.db.transaction() as session:
                job = session.get(Job, spec.job_id)
                assert job is not None
                return JobState(job.state)

        return SubmissionResponse(spec=spec, state=await database_call(state))

    async def ticket(self, job_id: UUID, principal: Principal) -> UploadTicket:
        ticket = await database_call(self.uploads.ticket, job_id, principal)
        if ticket.grant:
            await self.transfers.gateway.register(ticket.grant)
            try:
                await database_call(self.uploads.recheck, job_id, principal)
            except Exception:
                await self.transfers.gateway.revoke(ticket.grant.transfer_id)
                raise
        return ticket
