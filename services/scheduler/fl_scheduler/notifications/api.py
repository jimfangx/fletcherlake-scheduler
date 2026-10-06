"""Administrators inspect safe delivery metadata and explicitly retry failed intent."""

import asyncio
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_common.models.scheduler import Principal
from sqlalchemy import select

from ..auth.api import AuthAPI
from ..db.core import Database
from ..db.models import Event, Notification


class Administration:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def authorize(principal: Principal) -> None:
        if not principal.permits("system:admin"):
            raise PlatformError("FORBIDDEN", "Administrator role required")

    def list(self, principal: Principal, limit: int, offset: int) -> list[dict[str, Any]]:
        self.authorize(principal)
        with self.db.transaction() as session:
            rows = session.scalars(
                select(Notification)
                .order_by(Notification.event_id.desc(), Notification.notification_id)
                .limit(limit)
                .offset(offset)
            )
            return [
                {
                    "notification_id": str(row.notification_id),
                    "event_id": row.event_id,
                    "channel": row.channel,
                    "state": row.state,
                    "attempts": row.attempts,
                    "error_code": row.error,
                    "http_status": row.last_status,
                    "next_attempt_at": row.next_attempt_at.isoformat(),
                    "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                    "job_id": row.payload.get("job_id") if row.payload else None,
                    "event_type": row.payload.get("event_type") if row.payload else None,
                }
                for row in rows
            ]

    def retry(self, notification_id: UUID, principal: Principal) -> None:
        self.authorize(principal)
        with self.db.transaction() as session:
            row = session.get(Notification, notification_id, with_for_update=True)
            if row is None:
                raise PlatformError("NOTIFICATION_NOT_FOUND", "Unknown notification")
            if row.route_id is None or row.payload is None:
                raise PlatformError(
                    "NOTIFICATION_UNROUTED", "Legacy notification has no delivery scope"
                )
            if row.state not in {"FAILED", "SKIPPED"}:
                return
            row.state, row.error = "PENDING", None
            row.budget_start = row.attempts
            row.lease_id = row.lease_until = None
            row.next_attempt_at = utcnow()
            session.add(
                Event(
                    type="NOTIFICATION_RETRY_REQUESTED",
                    payload={
                        "notification_id": str(notification_id),
                        "requested_by": principal.email,
                    },
                )
            )


def routes(auth: AuthAPI, db: Database) -> APIRouter:
    router = APIRouter(prefix="/api/admin/notifications", tags=["notifications"])
    administration = Administration(db)

    @router.get("")
    async def notifications(
        principal: Annotated[Principal, Depends(auth.principal)],
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(administration.list, principal, limit, offset)

    @router.post("/{notification_id}/retry", status_code=202)
    async def retry(
        notification_id: UUID, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> dict[str, str]:
        await asyncio.to_thread(administration.retry, notification_id, principal)
        return {"notification_id": str(notification_id), "status": "retry_requested"}

    return router
