"""Local command surface; all job operations use the same SDK and JobConfig."""

import json
import os
from pathlib import Path
from uuid import UUID

import typer
from fl import Cluster
from fl_common.models import JobConfig

from . import lifecycle, setup

app = typer.Typer(no_args_is_help=True)
cluster_app = typer.Typer(no_args_is_help=True)
job_app = typer.Typer(no_args_is_help=True)
app.add_typer(cluster_app, name="cluster")
app.add_typer(job_app, name="job")
cluster_app.add_typer(setup.app, name="setup")
cluster_app.command()(lifecycle.restart)
cluster_app.command()(lifecycle.destroy)


def local() -> Cluster:
    return Cluster.local(Path(os.environ.get("FL_AGENT_SOCKET", "/var/run/fl/agent.sock")))


@cluster_app.command()
def status(dashboard: bool = False) -> None:
    client = local()
    try:
        if dashboard:
            from .tui import Dashboard

            Dashboard(client).run()
        else:
            typer.echo(json.dumps(client.status(), indent=2))
    finally:
        client.close()


@job_app.command()
def submit(
    config: Path,
    board: str | None = None,
    force_reflash: bool = False,
    timeout: int | None = None,
) -> None:
    parsed = JobConfig.from_yaml(config)
    if board:
        parsed.resource_constraints.board = board
    if force_reflash:
        parsed.force_reflash = True
    if timeout is not None:
        parsed.run_timeout = timeout
    client = local()
    try:
        typer.echo(client.submit(parsed).spec.job_id)
    finally:
        client.close()


@job_app.command()
def kill(job_id: UUID) -> None:
    client = local()
    try:
        typer.echo(client.kill(job_id).state)
    finally:
        client.close()


@job_app.command(name="status")
def job_status(job_id: UUID) -> None:
    client = local()
    try:
        typer.echo(client.job(job_id).model_dump_json(indent=2))
    finally:
        client.close()
