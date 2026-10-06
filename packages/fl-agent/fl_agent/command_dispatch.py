"""Bounded concurrent commands keep cancellation responsive during long payload transfers."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.protocol import Ack, Message, MessageType
from fl_common.protocol.limits import MAX_ACTIVE_COMMANDS


class CommandDispatcher:
    def __init__(
        self,
        handle: Callable[[Message], Awaitable[Ack]],
        send: Callable[[Message], Awaitable[None]],
    ) -> None:
        self.handle, self.send = handle, send
        self.inflight: dict[UUID, tuple[Message, asyncio.Task[None]]] = {}
        self.failed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def submit(self, message: Message) -> None:
        for message_id, (_, task) in list(self.inflight.items()):
            if task.done():
                await task
                del self.inflight[message_id]
        if previous := self.inflight.get(message.message_id):
            if previous[0] != message:
                raise PlatformError(
                    "PROTOCOL_ERROR", "Active message ID reused with different content"
                )
            return
        if len(self.inflight) >= MAX_ACTIVE_COMMANDS:
            raise PlatformError("COMMAND_BACKLOG", "Too many active agent commands")
        task = asyncio.create_task(self.execute(message))
        self.inflight[message.message_id] = message, task
        task.add_done_callback(self.completed)

    async def execute(self, message: Message) -> None:
        ack = await self.handle(message)
        await self.send(Message(type=MessageType.ACK, payload=ack.model_dump(mode="json")))

    def completed(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) and not self.failed.done():
            self.failed.set_exception(error)

    async def watch(self) -> None:
        await self.failed

    async def stop(self) -> None:
        tasks = [task for _, task in self.inflight.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.inflight.clear()
        if not self.failed.done():
            self.failed.cancel()
        elif not self.failed.cancelled():
            self.failed.exception()
