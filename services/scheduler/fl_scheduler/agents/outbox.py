"""Bound delivery bursts and allow cancellation to pass long transfers after staging."""

from uuid import UUID

from fl_common.protocol import Message, MessageType
from fl_common.protocol.limits import MAX_ACTIVE_COMMANDS
from sqlalchemy import case, exists, select
from sqlalchemy.orm import aliased

from ..db.core import Database
from ..db.models import Command


def pending_commands(db: Database, cluster_id: UUID, limit: int | None) -> list[Message]:
    statement = select(Command).where(
        Command.cluster_id == cluster_id, Command.acknowledged_at.is_(None)
    )
    if limit is not None:
        if not 1 <= limit <= MAX_ACTIVE_COMMANDS:
            raise ValueError("Command batch is outside the agent capacity")
        predecessor = aliased(Command)
        # Cancellation can pass a blocked fetch/publication. It must not pass the
        # unacknowledged staging/enqueue that creates the corresponding local job:
        # otherwise JOB_NOT_FOUND would become a durable failed cancellation receipt.
        creating = exists(
            select(predecessor.message_id).where(
                predecessor.cluster_id == cluster_id,
                predecessor.job_id == Command.job_id,
                predecessor.acknowledged_at.is_(None),
                predecessor.envelope["type"]
                .as_string()
                .in_(
                    [
                        MessageType.JOB_STAGE.value,
                        MessageType.JOB_ENQUEUE.value,
                    ]
                ),
            )
        )
        urgent = (Command.envelope["type"].as_string() == MessageType.JOB_CANCEL.value) & ~creating
        statement = statement.order_by(case((urgent, 0), else_=1)).limit(limit)
    statement = statement.order_by(Command.created_at, Command.message_id)
    with db.transaction() as session:
        return [Message.model_validate(command.envelope) for command in session.scalars(statement)]
