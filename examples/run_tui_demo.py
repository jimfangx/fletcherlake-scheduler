"""Serve a populated local agent with mock boards and disposable demo inputs."""

import argparse
import asyncio
import os
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import uvicorn
import yaml
from fastapi import FastAPI
from fl_agent.api import create_app
from fl_agent.configuration import confirm_config, write_config
from fl_agent.hardware.mock import MockBehavior, MockBoardBackend
from fl_agent.service import AgentService
from fl_common.models import ClusterConfig, JobConfig, ResourceConstraints


async def wait_for_job(service: AgentService, job_id: UUID, *, terminal: bool) -> None:
    async with asyncio.timeout(10):
        while True:
            job = service.db.get(job_id)
            if (terminal and job.state.terminal) or (not terminal and job.state == "RUNNING"):
                return
            await asyncio.sleep(0.02)


async def seed_jobs(service: AgentService, backends: dict[str, MockBoardBackend]) -> None:
    inputs = service.state_root / "demo-inputs"
    inputs.mkdir(mode=0o700)
    binary, bitstream = inputs / "hello.elf", inputs / "top.bit"
    binary.write_bytes(b"mock ELF input\n" * 256)
    bitstream.write_bytes(b"mock FPGA bitstream\n" * 4096)
    boards = list(backends)

    def config(board: str, *, priority: int = 0, ttl: int = 30) -> JobConfig:
        return JobConfig(
            resource_constraints=ResourceConstraints(board=board),
            binary=str(binary),
            bitstream=str(bitstream),
            priority=priority,
            run_collateral_ttl=ttl,
            run_timeout=3600,
        )

    # Finish real mock executions first, so results/logs and retention are visible.
    for passed, ttl in ((True, 30), (True, 7), (False, 1)):
        backends[boards[0]].behavior.passed = passed
        job = await service.submit(config(boards[0], ttl=ttl))
        await wait_for_job(service, job.spec.job_id, terminal=True)

    # Keep each board busy while adding work behind it. Canceling a running job
    # through the usual CLI advances its queue; every new run lasts ten minutes.
    for board, backend in backends.items():
        backend.behavior.passed = True
        backend.behavior.run_seconds = 600
        job = await service.submit(config(board))
        await wait_for_job(service, job.spec.job_id, terminal=False)
    for board in boards:
        for priority in (5, -5, 0):
            await service.submit(config(board, priority=priority))


def create_demo(state_root: Path) -> FastAPI:
    config = ClusterConfig.model_validate(
        yaml.safe_load(Path(__file__).with_name("cluster.yaml").read_text())
    )
    boards = [board for board in config.boards if board is not None]
    if len(boards) != 2 or any(board.backend != "mock" for board in boards):
        raise ValueError("The TUI demo requires exactly two explicit mock boards")
    state_root.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_config(state_root, config)
    confirm_config(state_root)
    runtime_root = state_root / "run"
    runtime_root.mkdir(mode=0o700)
    backends = {
        board.board_id: MockBoardBackend(
            MockBehavior(run_seconds=0.1, uart=b"mock: boot\nmock: hello from Fletcherlake\n")
        )
        for board in boards
    }
    service = AgentService(
        config, state_root, runtime_root, {board: backend for board, backend in backends.items()}
    )
    app = create_app(service)
    agent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with agent_lifespan(app):
            await seed_jobs(service, backends)
            print("Demo ready: 2 succeeded, 1 failed, 2 running, 6 queued jobs.", flush=True)
            print("In another terminal, from the repository root, run:", flush=True)
            print(
                f"FL_AGENT_SOCKET={shlex.quote(str(runtime_root / 'agent.sock'))} "
                "pixi run fl cluster status --dashboard",
                flush=True,
            )
            yield

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=Path("/tmp/fl-tui-populated"))
    args = parser.parse_args()
    os.umask(0o077)
    root = args.state_root.expanduser().resolve()
    try:
        app = create_demo(root)
    except FileExistsError:
        parser.error("State directory already exists; choose a fresh --state-root")
    uvicorn.run(app, uds=str(root / "run" / "agent.sock"))


if __name__ == "__main__":
    main()
