"""Slow snapshot ACKs must bound transmission without blocking incoming commands."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import MagicMock
from uuid import uuid4

from fl_agent.connection import SchedulerConnection
from fl_agent.service import AgentService
from fl_common.protocol import Ack, Message, MessageType


async def test_snapshot_waits_for_matching_ack_but_commands_remain_responsive(monkeypatch):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()

    class Socket:
        async def send(self, text):
            message = Message.model_validate_json(text)
            if message.type == MessageType.HELLO:
                await incoming.put(
                    Message(type=MessageType.REGISTER_ACK, payload={"event_sequence": 0})
                )
            else:
                await outgoing.put(message)

        async def recv(self):
            return (await incoming.get()).model_dump_json()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield Socket()

    monkeypatch.setattr("fl_agent.connection.connect", connect)
    service = MagicMock(spec=AgentService)
    service.config = MagicMock(cluster_id=uuid4())
    service.db = MagicMock()
    service.db.events.return_value = []
    service.snapshot.return_value = {"test": "snapshot"}
    link = SchedulerConnection(
        service, "https://scheduler.test", "test-token", heartbeat_seconds=0.01
    )

    async def handle(message):
        return Ack(message_id=message.message_id, accepted=True)

    monkeypatch.setattr(link.handler, "handle", handle)
    operation = asyncio.create_task(link._session())
    try:
        first = await asyncio.wait_for(outgoing.get(), 1)
        assert first.type == MessageType.STATUS_SNAPSHOT
        command = Message(type=MessageType.STATUS_REQUEST)
        await incoming.put(command)
        response = await asyncio.wait_for(outgoing.get(), 1)
        assert response.type == MessageType.ACK
        assert Ack.model_validate(response.payload).message_id == command.message_id
        # Unrelated/stale acknowledgements must not open the snapshot window.
        await incoming.put(
            Message(
                type=MessageType.ACK,
                payload=Ack(
                    message_id=uuid4(),
                    accepted=True,
                    result={"event_sequence": 500},
                ).model_dump(mode="json"),
            )
        )
        await asyncio.sleep(0.06)
        assert outgoing.empty()
        await incoming.put(
            Message(
                type=MessageType.ACK,
                payload=Ack(
                    message_id=first.message_id,
                    accepted=True,
                    result={"event_sequence": 7},
                ).model_dump(mode="json"),
            )
        )
        next_snapshot = await asyncio.wait_for(outgoing.get(), 1)
        assert next_snapshot.type == MessageType.STATUS_SNAPSHOT
        assert service.db.events.call_args.args == (7,)
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
