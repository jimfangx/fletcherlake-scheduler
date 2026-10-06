"""Subprocess test harness: real daemon, deterministic firmware delays, no test-only API."""

import asyncio
import json
import sys
from pathlib import Path

import uvicorn
from fl_agent.api import create_app
from fl_agent.configuration import load_config
from fl_agent.hardware.mock import MockBehavior, MockBoardBackend
from fl_agent.service import AgentService


class SlowStopBoard(MockBoardBackend):
    async def stop(self) -> None:
        if self.running:
            await asyncio.sleep(0.5)
        await super().stop()


if __name__ == "__main__":
    state_root, runtime_root = Path(sys.argv[1]), Path(sys.argv[2])
    behavior = MockBehavior(**json.loads(sys.argv[3]))
    config = load_config(state_root)
    boards = {
        board.board_id: SlowStopBoard(behavior) for board in config.boards if board is not None
    }
    service = AgentService(config, state_root, runtime_root, boards)
    uvicorn.run(create_app(service), uds=str(runtime_root / "agent.sock"), log_level="warning")
