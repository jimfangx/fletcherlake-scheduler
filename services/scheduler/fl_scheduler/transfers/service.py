"""Advance durable delivery using private HTTPS control and agent ACKs, never SSH."""

import asyncio
import hashlib
import logging
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_common.models.scheduler import Principal
from fl_common.models.transfer import TransferEndpoint, TransferGrant
from fl_common.protocol.delivery import FetchCommand, StageReceipt

from ..agents.commands import Commands
from ..db.async_calls import database_call
from ..db.core import Database
from .gateway import GatewayControl
from .queries import Work
from .state import DeliveryState

logger = logging.getLogger(__name__)


class TransferService:
    def __init__(
        self,
        db: Database,
        gateway: GatewayControl,
        endpoint: TransferEndpoint,
        *,
        simulation_networks: tuple[str, ...] = (),
    ) -> None:
        self.state = DeliveryState(db)
        self.commands = Commands(db)
        self.gateway, self.endpoint = gateway, endpoint
        self.simulation_networks = simulation_networks

    async def begin(self, job_id: UUID, upload_id: UUID, principal: Principal) -> UUID:
        await database_call(self.state.authorize, job_id, principal)
        source = await self.gateway.status(upload_id)
        if source.state != "VERIFIED" or source.grant.transfer_id != upload_id:
            raise PlatformError("TRANSFER_INCOMPLETE", "Gateway must attest a verified upload")
        return await database_call(self.state.begin, job_id, principal, source.grant)

    async def fail(self, work: Work, reason: str) -> None:
        await database_call(self.state.cancel, work.spec.job_id, reason)
        await self.gateway.revoke(work.delivery.transfer_id)

    async def advance(self, job_id: UUID) -> None:
        work = await database_call(self.state.work, job_id)
        if work.terminal:
            await self.gateway.revoke(work.delivery.transfer_id)
            await database_call(self.state.done, job_id)
            return
        if work.canceled or work.delivery.state == "CANCELING":
            await self.fail(work, "JOB_CANCELED")
            return
        if work.delivery.state == "ENQUEUING":
            return
        if work.delivery.deadline <= utcnow():
            await self.fail(work, "TRANSFER_TIMED_OUT")
            return
        if work.stage is None:
            return
        if not work.stage.accepted:
            await self.fail(work, work.stage.error_code or "TRANSFER_STAGE_FAILED")
            return
        receipt = StageReceipt.model_validate(work.stage.result)
        if (
            receipt.transfer_id != work.delivery.transfer_id
            or receipt.record.spec != work.spec
            or receipt.record.board_id != work.assignment.board_id
        ):
            await self.fail(work, "TRANSFER_STAGE_CONFLICT")
            return
        source = TransferGrant.model_validate(work.delivery.source)
        if work.fetch and work.fetch.accepted:
            if work.fetch.result != {
                "transfer_id": str(work.delivery.transfer_id),
                "files": [ref.model_dump(mode="json") for ref in source.files],
            }:
                await self.fail(work, "TRANSFER_RECEIPT_CONFLICT")
                return
            await database_call(self.state.ready, job_id)
            await database_call(self.commands.enqueue_after_transfer, job_id)
            return
        if work.fetch and work.fetch.error_code not in {
            "TRANSFER_FAILED",
            "TRANSFER_EXPIRED",
            "TRANSFER_IO_FAILED",
            "RCLONE_MISSING",
        }:
            await self.fail(work, work.fetch.error_code or "TRANSFER_FETCH_FAILED")
            return
        if work.fetching or work.delivery.next_attempt_at > utcnow():
            return
        if work.delivery.attempts >= 10:
            await self.fail(work, "TRANSFER_RETRIES_EXHAUSTED")
            return
        networks = work.networks or self.simulation_networks
        if not networks:
            await self.fail(work, "TRANSFER_PRIVATE_ADDRESS")
            return
        grant = TransferGrant(
            transfer_id=work.delivery.transfer_id,
            job_id=job_id,
            direction="download",
            public_key=receipt.public_key,
            token_hash=hashlib.sha256(b"private download uses SSH identity").hexdigest(),
            files=source.files,
            expires_at=work.delivery.deadline,
            retains_until=source.retains_until,
            source_id=source.transfer_id,
            source_networks=list(networks),
        )
        await self.gateway.register(grant)
        await database_call(
            self.state.fetch, FetchCommand(job_id=job_id, endpoint=self.endpoint, grant=grant)
        )

    async def tick(self) -> None:
        for job_id in await database_call(self.state.pending):
            try:
                await self.advance(job_id)
            except Exception as error:
                logger.error("Transfer delivery retry (%s)", type(error).__name__)
            finally:
                await database_call(self.state.defer, job_id)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as error:
                logger.error("Transfer delivery scan failed (%s)", type(error).__name__)
            await asyncio.sleep(2)
