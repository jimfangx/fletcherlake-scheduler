"""Reusable real scheduler WebSocket server and three connected mock Mac agents."""

import asyncio
import socket
from collections.abc import Callable
from uuid import UUID

import pytest
import uvicorn
from fastapi import FastAPI
from fl_agent.configuration import confirm_config, write_config
from fl_agent.connection import SchedulerConnection
from fl_agent.service import AgentService
from fl_scheduler.agents.gateway import AgentGateway
from fl_scheduler.db.async_calls import database_call
from fl_scheduler.db.models import Job
from fl_scheduler.registry import Registry


async def until(
    predicate: Callable[[], bool],
    seconds: float = 15,
    *,
    poll_in_thread: bool = False,
    description: str = "condition",
) -> None:
    try:
        async with asyncio.timeout(seconds):
            while True:
                ready = await database_call(predicate) if poll_in_thread else predicate()
                if ready:
                    return
                await asyncio.sleep(0.02)
    except TimeoutError as error:
        raise TimeoutError(f"Timed out after {seconds}s waiting for {description}") from error


class GatewayServer:
    def __init__(self, db, port: int = 0, *, ssl_certfile=None, ssl_keyfile=None) -> None:
        app = FastAPI()
        self.gateway = AgentGateway(db)
        app.include_router(self.gateway.router)
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", port))
        self.port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="error",
                timeout_graceful_shutdown=5,
                ssl_certfile=ssl_certfile,
                ssl_keyfile=ssl_keyfile,
            )
        )
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        await until(lambda: self.server.started)

    async def stop(self) -> None:
        self.server.should_exit = True
        if self.task:
            await self.task
        self.socket.close()


@pytest.fixture
async def connected_agents(scheduler_db, config, tmp_path):
    server = GatewayServer(scheduler_db)
    await server.start()
    services = []
    try:
        for index in range(3):
            inventory = config.model_copy(deep=True)
            for board in inventory.boards:
                if board:
                    board.board_id = f"mac-{index}-{board.board_id}"
            token = f"test-agent-token-{index}"
            registered = Registry(scheduler_db).register(inventory, token)
            root = tmp_path / f"mac-{index}"
            write_config(root, registered)
            confirm_config(root)
            service = AgentService(registered, root, root / "run")
            service.scheduler_link = SchedulerConnection(
                service,
                f"http://127.0.0.1:{server.port}",
                token,
                allow_http=True,
                heartbeat_seconds=0.05,
            )
            services.append(service)
            await service.start()
        await until(lambda: all(service.scheduler_link.connected for service in services))
        yield server, services
    finally:
        for service in services:
            await service.stop()
        await server.stop()


def state(db, job_id: UUID) -> str:
    with db.transaction() as session:
        return session.get(Job, job_id).state
