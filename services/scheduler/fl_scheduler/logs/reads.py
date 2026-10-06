"""Deduplicate bounded reads using the existing durable command outbox."""

from datetime import timedelta
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_common.models.logs import MAX_LOG_BYTES, LogChunk, LogPage, LogStream
from fl_common.models.scheduler import Principal
from fl_common.protocol import Ack, Message, MessageType
from fl_common.protocol.logs import LogRead
from sqlalchemy import select

from ..api.control import add_command
from ..api.queries import owned_job
from ..db.core import Database
from ..db.models import Command
from .heads import current


class LogReads:
    def __init__(self, db: Database) -> None:
        self.db = db

    def page(
        self,
        job_id: UUID,
        principal: Principal,
        stream: LogStream,
        offset: int,
        byte_limit: int = MAX_LOG_BYTES,
    ) -> LogPage:
        with self.db.transaction(placement=True) as session:
            job = owned_job(session, job_id, principal)
            head, cluster_id = current(session, job, stream)
            waiting = LogPage(
                job_id=job_id, stream=stream, offset=offset, state="WAITING", head=head
            )
            if head is None:
                return waiting
            if offset > head.size_bytes:
                raise PlatformError("LOG_OFFSET", "Offset exceeds the advertised log size")
            if offset == head.size_bytes:
                return LogPage(
                    job_id=job_id,
                    stream=stream,
                    offset=offset,
                    state="READY",
                    head=head,
                    chunk=LogChunk.build(head, offset, b""),
                )
            end = min(offset + byte_limit, head.size_bytes)
            rows = session.scalars(
                select(Command)
                .where(
                    Command.job_id == job_id,
                    Command.envelope["type"].as_string() == MessageType.LOG_READ,
                    Command.created_at > utcnow() - timedelta(seconds=30),
                )
                .order_by(Command.created_at.desc())
            )
            for row in rows:
                request = LogRead.model_validate(row.envelope["payload"])
                if (
                    request.head.stream != stream
                    or request.head.file_id != head.file_id
                    or request.offset != offset
                    or min(request.offset + request.byte_limit, request.head.size_bytes) != end
                    or request.expires_at <= utcnow()
                ):
                    continue
                if row.response is None:
                    return waiting
                ack = Ack.model_validate(row.response)
                if not ack.accepted:
                    if ack.error_code == "ARTIFACT_EXPIRED":
                        raise PlatformError("ARTIFACT_EXPIRED", "Agent log retention ended")
                    continue
                chunk = LogChunk.model_validate(ack.result)
                if (
                    chunk.next_offset != end
                    or chunk.offset != offset
                    or chunk.file_id != head.file_id
                ):
                    raise PlatformError("LOG_CHANGED", "Agent returned an unexpected byte range")
                chunk.eof = head.terminal and chunk.next_offset == head.size_bytes
                return LogPage(
                    job_id=job_id,
                    stream=stream,
                    offset=offset,
                    state="READY",
                    head=head,
                    chunk=chunk,
                )
            assert cluster_id is not None
            # Bound simultaneous reads per job even while its agent is offline.
            pending = session.scalar(
                select(Command.message_id)
                .where(
                    Command.job_id == job_id,
                    Command.envelope["type"].as_string() == MessageType.LOG_READ,
                    Command.acknowledged_at.is_(None),
                    Command.created_at > utcnow() - timedelta(seconds=30),
                )
                .offset(15)
                .limit(1)
            )
            if pending:
                raise PlatformError("SLOW_DOWN", "Too many pending log reads for this job")
            request = LogRead(
                head=head,
                offset=offset,
                byte_limit=byte_limit,
                expires_at=utcnow() + timedelta(seconds=30),
            )
            add_command(
                session,
                cluster_id,
                Message(type=MessageType.LOG_READ, payload=request.model_dump(mode="json")),
                job_id,
            )
            return waiting
