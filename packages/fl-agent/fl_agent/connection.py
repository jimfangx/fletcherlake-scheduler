"""One outbound scheduler connection; its failure never cancels board workers."""

import asyncio
import logging
from urllib.parse import urlparse, urlunparse
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.protocol import Ack, Message, MessageType
from websockets.asyncio.client import connect

from .command_dispatch import CommandDispatcher
from .commands import CommandHandler
from .service import AgentService

logger = logging.getLogger(__name__)


def websocket_url(scheduler_url: str, cluster_id: str, allow_http: bool = False) -> str:
    parsed = urlparse(scheduler_url)
    if parsed.scheme != "https" and not (allow_http and parsed.scheme == "http"):
        raise ValueError(
            "Scheduler URL must use HTTPS; HTTP is allowed only in explicit simulation"
        )
    if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Scheduler URL cannot contain credentials, query, or fragment")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/") + f"/api/agents/{cluster_id}/ws"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


class SchedulerConnection:
    def __init__(
        self,
        service: AgentService,
        scheduler_url: str,
        agent_token: str,
        *,
        allow_http: bool = False,
        heartbeat_seconds: float = 5,
    ) -> None:
        if service.config.cluster_id is None:
            raise ValueError("Cluster must be enrolled before connecting to the scheduler")
        self.service = service
        self.url = websocket_url(scheduler_url, str(service.config.cluster_id), allow_http)
        self.agent_token = agent_token
        self.heartbeat_seconds = heartbeat_seconds
        self.handler = CommandHandler(service)
        self.connected = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="scheduler-connection")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.connected = False

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._session()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Log the failure category, avoiding URLs/credentials from library exceptions.
                logger.warning(
                    "Scheduler connection lost (%s); local jobs continue",
                    error.code if isinstance(error, PlatformError) else type(error).__name__,
                )
            finally:
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)

    async def _session(self) -> None:
        tasks: list[asyncio.Task[None]] = []
        async with connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.agent_token}"},
            open_timeout=15,
            max_size=64 * 1024 * 1024,
        ) as socket:
            hello = Message(type=MessageType.HELLO, payload={"snapshot": self.service.snapshot()})
            await socket.send(hello.model_dump_json())
            async with asyncio.timeout(15):
                registered = Message.model_validate_json(await socket.recv())
            if registered.type != MessageType.REGISTER_ACK:
                raise PlatformError("PROTOCOL_ERROR", "Scheduler did not acknowledge registration")
            cursor = int(registered.payload["event_sequence"])
            self.connected = True
            write_lock = asyncio.Lock()
            pending_snapshot: tuple[UUID, asyncio.Future[None]] | None = None

            async def send(message: Message) -> None:
                async with write_lock:
                    await socket.send(message.model_dump_json())

            async def heartbeats() -> None:
                nonlocal pending_snapshot
                while True:
                    events = self.service.db.events(cursor)
                    message = Message(
                        type=MessageType.STATUS_SNAPSHOT,
                        payload={
                            "snapshot": self.service.snapshot(),
                            "events": [event.model_dump(mode="json") for event in events],
                        },
                    )
                    acknowledged = asyncio.get_running_loop().create_future()
                    pending_snapshot = message.message_id, acknowledged
                    try:
                        await send(message)
                        # One unacknowledged snapshot bounds buffers when projection is slow.
                        # The independent receiver still accepts hardware cancellation.
                        async with asyncio.timeout(30):
                            await acknowledged
                    finally:
                        pending_snapshot = None
                    await asyncio.sleep(self.heartbeat_seconds)

            dispatcher = CommandDispatcher(self.handler.handle, send)

            async def receive() -> None:
                nonlocal cursor
                while True:
                    message = Message.model_validate_json(await socket.recv())
                    if message.type == MessageType.ACK:
                        ack = Ack.model_validate(message.payload)
                        if pending_snapshot and ack.message_id == pending_snapshot[0]:
                            waiter = pending_snapshot[1]
                            if not waiter.done():
                                if ack.accepted and "event_sequence" in ack.result:
                                    cursor = max(cursor, int(ack.result["event_sequence"]))
                                    waiter.set_result(None)
                                else:
                                    waiter.set_exception(
                                        PlatformError(
                                            "SNAPSHOT_REJECTED",
                                            "Scheduler rejected the state snapshot",
                                        )
                                    )
                    else:
                        await dispatcher.submit(message)

            try:
                tasks = [
                    asyncio.create_task(heartbeats()),
                    asyncio.create_task(receive()),
                    asyncio.create_task(dispatcher.watch()),
                ]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await dispatcher.stop()
