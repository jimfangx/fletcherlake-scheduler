"""Retry upload revocation after terminal jobs and bounded gateway retention.

Keep rescanning until the issued grant expires: a registration already in flight
can finish after an early DELETE of an as-yet absent scope. A caller's post-register
check also revokes closed jobs, but process death cannot be that check's only guard.
"""

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

from fl_common.models import JobState
from fl_common.models.base import utcnow
from fl_common.models.transfer import TransferGrant
from sqlalchemy import select

from ..db.async_calls import database_call
from ..db.core import Database
from ..db.models import Job
from .gateway import GatewayControl
from .models import Upload

logger = logging.getLogger(__name__)


class UploadCleanup:
    def __init__(self, db: Database, gateway: GatewayControl) -> None:
        self.db, self.gateway = db, gateway

    def pending(self) -> list[UUID]:
        with self.db.transaction() as session:
            return list(
                session.scalars(
                    select(Upload.job_id)
                    .where(Upload.closed.is_(False), Upload.next_cleanup_at <= utcnow())
                    .order_by(Upload.next_cleanup_at, Upload.job_id)
                    .limit(100)
                )
            )

    def scope(self, job_id: UUID) -> TransferGrant | None:
        with self.db.transaction(placement=True) as session:
            upload = session.get(Upload, job_id)
            job = session.get(Job, job_id)
            assert upload is not None and job is not None
            if upload.grant is None:
                if job.cancel_requested or JobState(job.state).terminal:
                    upload.closed = True
                return None
            grant = TransferGrant.model_validate(upload.grant)
            if (
                job.cancel_requested
                or JobState(job.state).terminal
                or grant.retains_until <= utcnow()
            ):
                return grant
            return None

    def defer(self, job_id: UUID, revoked: TransferGrant | None) -> None:
        with self.db.transaction(placement=True) as session:
            upload = session.get(Upload, job_id)
            assert upload is not None
            upload.next_cleanup_at = utcnow() + timedelta(seconds=2)
            # Re-read the persisted scope: an owner may have renewed it during IO.
            current = TransferGrant.model_validate(upload.grant) if upload.grant else None
            if revoked and current and current.expires_at <= utcnow():
                upload.closed = True

    async def tick(self) -> None:
        for job_id in await database_call(self.pending):
            revoked = None
            try:
                grant = await database_call(self.scope, job_id)
                if grant:
                    await self.gateway.revoke(grant.transfer_id)
                    revoked = grant
            except Exception as error:
                logger.error("Upload revocation retry (%s)", type(error).__name__)
            finally:
                await database_call(self.defer, job_id, revoked)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as error:
                logger.error("Upload cleanup scan failed (%s)", type(error).__name__)
            await asyncio.sleep(2)
