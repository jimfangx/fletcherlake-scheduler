"""Authenticated WebSocket transport with durable command replay and state reconciliation."""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fl_common.errors import PlatformError
from fl_common.models import JobEvent
from fl_common.models.scheduler import ClusterSnapshot
from fl_common.protocol import Ack, Message, MessageType
from fl_common.protocol.limits import COMMAND_DELIVERY_BATCH
from pydantic import ValidationError

from ..db.core import Database
from ..db.models import Cluster
from ..registry import Registry
from .commands import Commands
from .reconcile import Reconciler

logger = logging.getLogger(__name__)


class AgentGateway:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.registry = Registry(db)
        self.reconciler = Reconciler(db)
        self.commands = Commands(db)
        self.router = APIRouter()
        self.router.add_api_websocket_route("/api/agents/{cluster_id}/ws", self.connect)

    def _cursor(self, cluster_id: UUID) -> int:
        with self.db.transaction() as session:
            cluster = session.get(Cluster, cluster_id)
            assert cluster is not None
            return cluster.event_sequence

    async def connect(self, websocket: WebSocket, cluster_id: UUID) -> None:
        authorization = websocket.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            await websocket.close(code=1008)
            return
        try:
            await asyncio.to_thread(self.registry.authenticate, cluster_id, authorization[7:])
        except PlatformError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            session_id = await asyncio.to_thread(self.reconciler.connected, cluster_id)
        except PlatformError:
            await websocket.close(code=1008)
            return
        tasks: list[asyncio.Task[None]] = []
        write_lock = asyncio.Lock()

        async def send(message: Message) -> None:
            async with write_lock:
                try:
                    await websocket.send_text(message.model_dump_json())
                except RuntimeError as error:
                    # ASGI transports can close while a database projection is in flight.
                    if "websocket.close" in str(error):
                        raise WebSocketDisconnect() from error
                    raise

        try:
            async with asyncio.timeout(15):
                hello = Message.model_validate_json(await websocket.receive_text())
            if hello.type != MessageType.HELLO:
                raise PlatformError("PROTOCOL_ERROR", "First agent message must be HELLO")
            snapshot = ClusterSnapshot.model_validate(hello.payload["snapshot"])
            await asyncio.to_thread(
                self.registry.update_config, cluster_id, snapshot.cluster, session_id=session_id
            )
            await asyncio.to_thread(self.reconciler.snapshot, cluster_id, session_id, snapshot)
            cursor = await asyncio.to_thread(self._cursor, cluster_id)
            await send(
                Message(
                    type=MessageType.REGISTER_ACK,
                    payload={
                        "message_id": str(hello.message_id),
                        "event_sequence": cursor,
                        "cluster_id": str(cluster_id),
                    },
                )
            )

            async def receive() -> None:
                while True:
                    message = Message.model_validate_json(await websocket.receive_text())
                    if message.type in {MessageType.STATUS_SNAPSHOT, MessageType.HEARTBEAT}:
                        snapshot = ClusterSnapshot.model_validate(message.payload["snapshot"])
                        await asyncio.to_thread(
                            self.reconciler.snapshot, cluster_id, session_id, snapshot
                        )
                        events = [
                            JobEvent.model_validate(event)
                            for event in message.payload.get("events", [])
                        ]
                        cursor = await asyncio.to_thread(
                            self.reconciler.events, cluster_id, session_id, events
                        )
                        await send(
                            Message(
                                type=MessageType.ACK,
                                payload=Ack(
                                    message_id=message.message_id,
                                    accepted=True,
                                    result={"event_sequence": cursor},
                                ).model_dump(mode="json"),
                            )
                        )
                    elif message.type == MessageType.ACK:
                        await asyncio.to_thread(
                            self.commands.acknowledge,
                            cluster_id,
                            Ack.model_validate(message.payload),
                            session_id,
                        )
                    else:
                        raise PlatformError(
                            "PROTOCOL_ERROR", f"Unsupported agent message {message.type}"
                        )

            async def replay_commands() -> None:
                while True:
                    for message in await asyncio.to_thread(
                        self.commands.pending, cluster_id, COMMAND_DELIVERY_BATCH
                    ):
                        await send(message)
                    await asyncio.sleep(1)

            tasks = [asyncio.create_task(receive()), asyncio.create_task(replay_commands())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except (WebSocketDisconnect, OSError):
            pass
        except (PlatformError, ValidationError, KeyError, TimeoutError) as error:
            logger.warning("Agent protocol rejected for cluster %s: %s", cluster_id, error)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            async with write_lock:
                try:
                    await websocket.close(code=1008)
                except (WebSocketDisconnect, OSError):
                    pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(self.reconciler.disconnected, cluster_id, session_id)
