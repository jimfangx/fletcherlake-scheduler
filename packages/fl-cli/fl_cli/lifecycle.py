"""Terminal entrypoints for shared Mac management; shutdown belongs to the daemon."""

from pathlib import Path

import typer
from fl import ClusterSetup
from fl_agent.configuration import DEFAULT_RUNTIME_ROOT, DEFAULT_STATE_ROOT

from .provisioning import ensure_root, require_macos


def restart(runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> None:
    require_macos()
    ensure_root()
    ClusterSetup(runtime_root=runtime_root).restart()


def destroy(
    state_root: Path = DEFAULT_STATE_ROOT,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    scheduler: str | None = None,
) -> None:
    require_macos()
    ensure_root()
    ClusterSetup(state_root, runtime_root).destroy(scheduler=scheduler)
    typer.echo(
        "Local cluster unregistered. History archived; retained collateral cleanup continues."
    )
