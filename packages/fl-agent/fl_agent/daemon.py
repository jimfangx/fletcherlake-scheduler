"""launchd entry point. No public TCP listener is supported by the local API."""

import logging
import os
from pathlib import Path

import typer
import uvicorn

from .api import create_app
from .configuration import DEFAULT_RUNTIME_ROOT, DEFAULT_STATE_ROOT, load_config
from .connection import SchedulerConnection
from .credentials import load_credentials
from .service import AgentService

app = typer.Typer()


@app.command()
def serve(
    state_root: Path = DEFAULT_STATE_ROOT,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> None:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    runtime_root.mkdir(parents=True, exist_ok=True)
    service = AgentService(load_config(state_root), state_root, runtime_root)
    credentials = load_credentials(state_root / "credentials.json")
    if credentials:
        service.scheduler_link = SchedulerConnection(
            service,
            credentials.scheduler_url,
            credentials.agent_token.get_secret_value(),
        )
    uvicorn.run(create_app(service), uds=str(runtime_root / "agent.sock"))


def main() -> None:
    app()
