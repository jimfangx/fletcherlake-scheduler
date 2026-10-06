"""Terminal prompts and options for the shared macOS lifecycle API."""

import json
import os
import shlex
from pathlib import Path
from typing import Annotated

import typer
from fl import ClusterSetup
from fl.macos.inventory import inventory, read_overrides
from fl_agent.configuration import DEFAULT_RUNTIME_ROOT, DEFAULT_STATE_ROOT
from fl_agent.detection import detect_host, merge_overrides
from fl_common.models import ClusterConfig

from .provisioning import ensure_root, require_macos, run

app = typer.Typer(no_args_is_help=True)


def parse_config(path: Path | None, interactive: bool = False) -> ClusterConfig:
    if path is None and not interactive:
        raise typer.BadParameter("Provide an inventory file or use --interactive")
    overrides = read_overrides(path) if path else {"boards": [None, None, None]}
    detected = detect_host()
    if interactive:
        from .inventory_editor import edit_inventory

        return edit_inventory(merge_overrides(detected, overrides))
    return inventory(overrides, detector=lambda: detected)


@app.command()
def init(
    config: Annotated[Path | None, typer.Argument(help="Optional inventory overrides")] = None,
    state_root: Path = DEFAULT_STATE_ROOT,
    interactive: bool = False,
    scheduler: str = typer.Option(..., help="Public HTTPS scheduler origin"),
    enrollment_token: str | None = typer.Option(None, show_default=False),
) -> None:
    require_macos()
    ensure_root()
    resolved = parse_config(config, interactive)
    if enrollment_token is None:
        enrollment_token = typer.prompt("Enrollment token", hide_input=True)
    ClusterSetup(state_root).init(resolved, scheduler=scheduler, enrollment_token=enrollment_token)
    typer.echo(f"Configuration saved: {state_root / 'cluster.yaml'}. Run setup confirm.")


@app.command()
def confirm(
    project: Path,
    state_root: Path = DEFAULT_STATE_ROOT,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> None:
    require_macos()
    ensure_root()
    result = ClusterSetup(state_root, runtime_root).confirm(project)
    typer.echo(json.dumps(result, indent=2))


@app.command()
def reconfigure(
    project: Path,
    config: Path | None = None,
    state_root: Path = DEFAULT_STATE_ROOT,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    interactive: bool = False,
    freeform: bool = False,
) -> None:
    require_macos()
    ensure_root()

    if freeform and (interactive or config):
        raise typer.BadParameter(
            "Use --freeform, --interactive, or --config; avoid combining editors"
        )

    def edit(target: Path) -> None:
        run([*shlex.split(os.environ.get("EDITOR", "vi")), str(target)])

    # Parsing and prompts run after the shared workflow persists the incomplete marker.
    ClusterSetup(state_root, runtime_root).reconfigure(
        project,
        editor=edit if freeform else None,
        config_factory=(lambda: parse_config(config or state_root / "cluster.yaml", interactive))
        if config or interactive
        else None,
    )
    typer.echo("Configuration confirmed; agent restarted.")
