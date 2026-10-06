"""Retry network cleanup independently of enrollment HTTP requests and hardware workers."""

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from sqlalchemy import select

from ..db.async_calls import database_call
from ..db.models import Event
from .enrollment import EnrollmentService
from .models import NetworkRevocation

logger = logging.getLogger(__name__)


class NetworkCleanup:
    def __init__(self, enrollment: EnrollmentService) -> None:
        self.enrollment = enrollment
        self.db, self.network = enrollment.db, enrollment.network

    def pending(self) -> list[tuple[UUID, str, str]]:
        with self.db.transaction() as session:
            return [
                (row.revocation_id, row.kind, row.object_id)
                for row in session.scalars(
                    select(NetworkRevocation)
                    .where(
                        NetworkRevocation.state == "PENDING",
                        NetworkRevocation.next_attempt_at <= utcnow(),
                    )
                    .order_by(NetworkRevocation.next_attempt_at)
                    .limit(100)
                )
            ]

    def receipt(self, revocation_id: UUID, error: str | None) -> None:
        with self.db.transaction() as session:
            row = session.get(NetworkRevocation, revocation_id, with_for_update=True)
            if row is None or row.state == "DONE":
                return
            row.attempts += 1
            row.error = error
            if error is None and (row.finish_after is None or row.finish_after <= utcnow()):
                row.state = "DONE"
                session.add(
                    Event(
                        cluster_id=row.cluster_id,
                        type="NETWORK_REVOKED",
                        payload={"kind": row.kind, "object_id": row.object_id},
                    )
                )
            else:
                row.next_attempt_at = utcnow() + timedelta(seconds=10 if error is None else 30)

    async def tick(self) -> None:
        await database_call(self.enrollment.tickets.expire)
        for revocation_id, kind, object_id in await database_call(self.pending):
            error = None
            try:
                if kind == "KEY":
                    await self.network.expire_key(object_id)
                    for node in await self.network.key_nodes(object_id):
                        await self.network.delete_node(node)
                else:
                    await self.network.delete_node(object_id)
            except Exception as failure:
                error = (
                    failure.code if isinstance(failure, PlatformError) else type(failure).__name__
                )
            await database_call(self.receipt, revocation_id, error)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as error:
                logger.error("Network cleanup failed (%s)", type(error).__name__)
            await asyncio.sleep(5)
