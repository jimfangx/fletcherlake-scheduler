"""Export terminal artifacts from Macs, seal on the gateway, and retry from durable state."""

import asyncio
import hashlib
import logging
from uuid import UUID

from fl_common.models.base import utcnow
from fl_common.models.transfer import TransferEndpoint, TransferGrant
from fl_common.protocol.exports import ExportReceipt, PublishCommand

from ..db.async_calls import database_call
from ..db.core import Database
from ..transfers.gateway import GatewayControl
from .queries import Work
from .state import ExportState

logger = logging.getLogger(__name__)


class ExportService:
    def __init__(
        self,
        db: Database,
        gateway: GatewayControl,
        endpoint: TransferEndpoint,
        *,
        simulation_networks: tuple[str, ...] = (),
    ) -> None:
        self.state, self.gateway, self.endpoint = ExportState(db), gateway, endpoint
        self.simulation_networks = simulation_networks

    async def close(self, work: Work, reason: str) -> None:
        await database_call(self.state.close, work.export.export_id, reason)
        await self.gateway.revoke(work.export.export_id)
        for download_id in await database_call(self.state.downloads, work.export.export_id):
            await self.gateway.revoke(download_id)
        await database_call(self.state.revoked, work.export.export_id)

    async def advance(self, export_id: UUID) -> None:
        work = await database_call(self.state.work, export_id)
        row = work.export
        if not work.valid or row.state == "CLOSING":
            await self.close(work, row.error or "ARTIFACT_EXPIRED")
            return
        if row.state == "READY":
            return
        if row.deadline <= utcnow():
            await self.close(work, "EXPORT_TIMED_OUT")
            return
        if work.prepare is None:
            return
        if not work.prepare.accepted:
            await self.close(work, work.prepare.error_code or "EXPORT_PREPARE_FAILED")
            return
        receipt = ExportReceipt.model_validate(work.prepare.result)
        if (
            receipt.job_id != row.job_id
            or receipt.transfer_id != export_id
            or receipt.artifacts != work.artifacts
        ):
            await self.close(work, "EXPORT_RECEIPT_CONFLICT")
            return
        if work.publish and work.publish.accepted:
            files = [record.ref for record in work.artifacts]
            if (
                work.publish.result
                != {
                    "transfer_id": str(export_id),
                    "files": [ref.model_dump(mode="json") for ref in files],
                }
                or await self.gateway.verify(export_id) != files
            ):
                await self.close(work, "EXPORT_VERIFICATION_FAILED")
                return
            await database_call(self.state.ready, export_id)
            return
        if work.publish and work.publish.error_code not in {
            "TRANSFER_FAILED",
            "TRANSFER_IO_FAILED",
            "BBCP_MISSING",
        }:
            await self.close(work, work.publish.error_code or "EXPORT_PUBLICATION_FAILED")
            return
        if work.publishing or row.next_attempt_at > utcnow():
            return
        if row.attempts >= 10:
            await self.close(work, "EXPORT_RETRIES_EXHAUSTED")
            return
        networks = work.networks or self.simulation_networks
        if not networks:
            await self.close(work, "TRANSFER_PRIVATE_ADDRESS")
            return
        grant = (
            TransferGrant.model_validate(row.grant)
            if row.grant
            else TransferGrant(
                transfer_id=export_id,
                job_id=row.job_id,
                direction="upload",
                public_key=receipt.public_key,
                token_hash=hashlib.sha256(
                    b"private export sealed by scheduler control"
                ).hexdigest(),
                files=[record.ref for record in work.artifacts],
                expires_at=row.deadline,
                retains_until=min(
                    record.expires_at for record in work.artifacts if record.expires_at
                ),
                source_networks=list(networks),
            )
        )
        await database_call(self.state.grant, export_id, grant)
        await self.gateway.register(grant)
        await database_call(
            self.state.publish,
            PublishCommand(job_id=row.job_id, endpoint=self.endpoint, grant=grant),
        )

    async def tick(self) -> None:
        for export_id in await database_call(self.state.pending):
            try:
                await self.advance(export_id)
            except Exception as error:
                logger.error("Artifact export retry (%s)", type(error).__name__)
            finally:
                await database_call(self.state.defer, export_id)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as error:
                logger.error("Artifact export scan failed (%s)", type(error).__name__)
            await asyncio.sleep(2)
