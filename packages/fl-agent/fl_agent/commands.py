"""Idempotent scheduler commands. ACKs are persisted only after durable effects."""

import asyncio
import json
from uuid import UUID
from weakref import WeakValueDictionary

from fl_common.errors import PlatformError
from fl_common.models import ClusterState
from fl_common.protocol import Ack, Message, MessageType
from fl_common.protocol.delivery import FetchCommand, StageCommand
from fl_common.protocol.exports import ExportCommand, PublishCommand
from fl_common.protocol.logs import LogRead

from .service import AgentService


class CommandHandler:
    def __init__(self, service: AgentService) -> None:
        self.service = service
        # Only concurrent callers need a lock; persisted receipts handle later replay.
        self.locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()

    async def handle(self, message: Message) -> Ack:
        lock = self.locks.setdefault(message.message_id, asyncio.Lock())
        async with lock:
            return await self._handle(message)

    async def _handle(self, message: Message) -> Ack:
        db = self.service.db
        stored = db.connection.execute(
            "SELECT response FROM processed_commands WHERE message_id=?",
            (str(message.message_id),),
        ).fetchone()
        if stored:
            receipt = json.loads(stored[0])
            if receipt["request"] != message.model_dump(mode="json"):
                return Ack(
                    message_id=message.message_id,
                    accepted=False,
                    error="Message ID reused with different content",
                )
            if message.type == MessageType.LOG_READ:
                try:
                    request = LogRead.model_validate(message.payload)
                    self.service.logs.check(
                        request, self.service.logs.head(request.head.job_id, request.head.stream)
                    )
                except (PlatformError, ValueError) as error:
                    return Ack(
                        message_id=message.message_id,
                        accepted=False,
                        error=str(error),
                        error_code=error.code
                        if isinstance(error, PlatformError)
                        else "INVALID_COMMAND",
                    )
            return Ack.model_validate(receipt["ack"])
        try:
            result = await self._execute(message)
            ack = Ack(message_id=message.message_id, accepted=True, result=result)
        except (PlatformError, ValueError, KeyError) as error:
            ack = Ack(
                message_id=message.message_id,
                accepted=False,
                error=str(error),
                error_code=error.code if isinstance(error, PlatformError) else "INVALID_COMMAND",
            )
        except OSError:
            ack = Ack(
                message_id=message.message_id,
                accepted=False,
                error="Transfer filesystem operation failed",
                error_code="TRANSFER_IO_FAILED",
            )
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO processed_commands(message_id,response,expires_at) VALUES (?,?,?)",
                (
                    str(message.message_id),
                    json.dumps(
                        {
                            "request": message.model_dump(mode="json"),
                            "ack": ack.model_dump(mode="json"),
                        }
                    ),
                    message.payload.get("expires_at")
                    if message.type == MessageType.LOG_READ
                    else None,
                ),
            )
        return ack

    async def _execute(self, message: Message) -> dict[str, object]:
        if message.type in {MessageType.JOB_STAGE, MessageType.JOB_ENQUEUE}:
            request = StageCommand.model_validate(message.payload)
            spec, board_id = request.spec, request.board_id
            if message.type == MessageType.JOB_STAGE:
                record = self.service.stage(spec, board_id)
                if request.transfer_id:
                    return self.service.transfers.prepare(record, request.transfer_id).model_dump(
                        mode="json"
                    )
            else:
                record = await self.service.enqueue(spec, board_id)
            return record.model_dump(mode="json")
        if message.type == MessageType.JOB_FETCH:
            return await self.service.transfers.fetch(FetchCommand.model_validate(message.payload))
        if message.type == MessageType.LOG_READ:
            return await self.service.logs.read(LogRead.model_validate(message.payload))
        if message.type == MessageType.ARTIFACT_PREPARE:
            return await self.service.exports.prepare(ExportCommand.model_validate(message.payload))
        if message.type == MessageType.ARTIFACT_PUBLISH:
            return await self.service.exports.publish(
                PublishCommand.model_validate(message.payload)
            )
        if message.type == MessageType.JOB_CANCEL:
            return self.service.cancel(UUID(str(message.payload["job_id"]))).model_dump(mode="json")
        if message.type == MessageType.ARTIFACT_DELETE:
            job_id = UUID(str(message.payload["job_id"]))
            self.service.store.request_deletion(job_id)
            await self.service.exports.cancel(job_id)
            self.service.store.sweep()
            return {"deletion_requested": True}
        if message.type == MessageType.STATUS_REQUEST:
            return self.service.snapshot()
        if message.type == MessageType.CLUSTER_DRAIN:
            await self.service.drain(ClusterState.DRAINING)
            return {"state": self.service.state}
        raise PlatformError("UNSUPPORTED_COMMAND", f"Unknown scheduler command {message.type}")
