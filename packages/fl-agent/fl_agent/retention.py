"""Hardware-free launchd maintenance entrypoint for permanently retired clusters."""

import logging
import os
from pathlib import Path
from typing import Annotated

import typer

from .configuration import DEFAULT_STATE_ROOT
from .retired import sweep_retired
from .retired_state import protected, retired_root

app = typer.Typer()
DEFAULT_RETIRED_ROOT = retired_root(DEFAULT_STATE_ROOT)


@app.command()
def sweep(
    retired_root_path: Annotated[Path, typer.Option("--retired-root")] = DEFAULT_RETIRED_ROOT,
    check: Annotated[
        bool, typer.Option(help="Check imports and protected storage without deleting files")
    ] = False,
) -> None:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    if check:
        protected(retired_root_path, directory=True)
        return
    report = sweep_retired(retired_root_path)
    logging.getLogger(__name__).info("Deleted %d expired job directories", report.deleted)
    if report.failed:
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
