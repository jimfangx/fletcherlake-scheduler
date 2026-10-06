"""Short-lived log RPC receipts cannot accumulate while an agent is offline."""

from datetime import timedelta

from fl_common.models.base import utcnow
from fl_common.protocol import Ack, MessageType
from fl_common.protocol.logs import LogRead
from sqlalchemy import select

from ..db.core import Database
from ..db.models import Command


def sweep(db: Database) -> None:
    with db.transaction(placement=True) as session:
        rows = session.scalars(
            select(Command).where(
                Command.envelope["type"].as_string() == MessageType.LOG_READ,
                Command.created_at <= utcnow() - timedelta(seconds=30),
            )
        )
        for row in rows:
            request = LogRead.model_validate(row.envelope["payload"])
            if request.expires_at > utcnow():
                continue
            if row.created_at <= utcnow() - timedelta(seconds=60):
                session.delete(row)
            elif row.acknowledged_at is None:
                row.acknowledged_at = utcnow()
                row.response = Ack(
                    message_id=row.message_id,
                    accepted=False,
                    error="Log request deadline expired",
                    error_code="LOG_REQUEST_EXPIRED",
                ).model_dump(mode="json")
