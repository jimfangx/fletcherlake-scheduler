"""Terminal entrypoints. Submission and artifact transport are separate modules."""

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import httpx
import typer
from fl_common.errors import PlatformError
from fl_common.models import JobConfig

from .api import RemoteClient
from .auth import login as approve_login
from .credentials import CredentialStore
from .monitor import Monitor
from .receipts import ReceiptStore
from .results import Results
from .session import ensure_login
from .submission import Submission

app = typer.Typer(help="Submit and monitor bringup jobs from any user host.")


@app.command()
def results(
    job_id: UUID,
    output: Path | None = None,
    inputs: bool = False,
    credentials: Path | None = None,
    scheduler: str | None = None,
) -> None:
    """Download terminal outputs through BBCP; repeating the same output directory resumes."""
    with errors(), RemoteClient(CredentialStore(credentials)) as client:
        ensure_login(client, scheduler, typer.echo)
        for path in Results(client, display=typer.echo).run(
            job_id,
            output or Path("results-" + str(job_id)),
            inputs=inputs,
        ):
            typer.echo(str(path))


@app.command()
def submit(
    config: Annotated[Path | None, typer.Argument()] = None,
    receipt: Path | None = None,
    resume: Path | None = None,
    credentials: Path | None = None,
    scheduler: str | None = None,
    priority: int | None = None,
    timeout: int | None = None,
    ttl: int | None = None,
    follow: bool = False,
) -> None:
    """Reserve, upload and submit through public APIs; --resume retries the saved request."""
    with errors(), RemoteClient(CredentialStore(credentials)) as client:
        if (config is None) == (resume is None) or (resume is not None and receipt is not None):
            raise ValueError(
                "Provide a job YAML or --resume RECEIPT, using a separate receipt for each job"
            )
        if resume is not None and any(value is not None for value in (priority, timeout, ttl)):
            raise ValueError("Resume preserves the original configuration; do not supply overrides")
        path = (
            resume
            or receipt
            or client.store.path.parent / "submissions" / str(uuid4()) / "receipt.json"
        )
        overrides: dict[str, object] = {
            key: value
            for key, value in {
                "priority": priority,
                "run_timeout": timeout,
                "run_collateral_ttl": ttl,
            }.items()
            if value is not None
        }
        parsed = JobConfig.from_yaml(config, overrides) if config else None
        ensure_login(client, scheduler, typer.echo)
        spec = Submission(client, display=typer.echo).run(ReceiptStore(path), parsed)
        typer.echo("Submitted " + str(spec.job_id))
        if follow:
            for state in Monitor(client).states(spec.job_id):
                typer.echo(state)


@contextmanager
def errors() -> Iterator[None]:
    try:
        yield
    except (PlatformError, httpx.HTTPError, OSError, ValueError) as error:
        message = error.message if isinstance(error, PlatformError) else type(error).__name__
        typer.echo(message, err=True)
        raise typer.Exit(1) from error


@app.command()
def login(scheduler: str = typer.Option(...), credentials: Path | None = None) -> None:
    """Print a URL and terminal code; no browser is needed on this machine."""
    with errors():
        approve_login(scheduler, CredentialStore(credentials), typer.echo)


@app.command()
def logout(credentials: Path | None = None) -> None:
    """Revoke this session and delete its local credentials."""
    with errors(), RemoteClient(CredentialStore(credentials)) as client:
        client.logout()
        typer.echo("Logged out")


@app.command()
def jobs(limit: int = 100, offset: int = 0, credentials: Path | None = None) -> None:
    """List your jobs; operators/admins can inspect all jobs."""
    with errors(), RemoteClient(CredentialStore(credentials)) as client:
        typer.echo(json.dumps(client.jobs(limit, offset), indent=2))


@app.command()
def status(
    job_id: UUID,
    credentials: Path | None = None,
    follow: bool = False,
    scheduler: str | None = None,
) -> None:
    """Read the scheduler's replicated job state."""
    with errors(), RemoteClient(CredentialStore(credentials)) as client:
        ensure_login(client, scheduler, typer.echo)
        if follow:
            for state in Monitor(client).states(job_id):
                typer.echo(state)
        else:
            typer.echo(json.dumps(client.status(job_id), indent=2))


@app.command()
def logs(
    job_id: UUID,
    stream: str = "stdout",
    offset: int = 0,
    follow: bool = False,
    credentials: Path | None = None,
    scheduler: str | None = None,
) -> None:
    """Write raw log bytes to stdout; --follow waits for new bytes through completion."""
    if stream not in {"stdout", "stderr"}:
        raise typer.BadParameter("Stream must be stdout or stderr")
    with errors(), RemoteClient(CredentialStore(credentials)) as client:
        ensure_login(client, scheduler, lambda line: typer.echo(line, err=True))
        for data in Monitor(client).logs(
            job_id,
            stream="stderr" if stream == "stderr" else "stdout",
            offset=offset,
            follow=follow,
        ):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


@app.command()
def cancel(job_id: UUID, credentials: Path | None = None) -> None:
    """Request durable cancellation; completion is reported by status."""
    with errors(), RemoteClient(CredentialStore(credentials)) as client:
        client.cancel(job_id)
        typer.echo("Cancellation requested for " + str(job_id))
