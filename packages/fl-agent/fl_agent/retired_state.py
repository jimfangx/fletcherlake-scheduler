"""Protected archive paths and the terminal-state prerequisites for hardware-free cleanup."""

import os
import stat
from pathlib import Path

from fl_common.errors import PlatformError
from fl_common.files import fsync_directory

from .db import AgentDB


def retired_root(state_root: Path) -> Path:
    return state_root.with_name(state_root.name + "-retired")


def protected(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PlatformError("RETIREMENT_PERMISSIONS", "Retired state must be owned and private")
    if directory and not path.is_dir():
        raise PlatformError("RETIREMENT_DIRECTORY", "Retired state must be a directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise PlatformError("RETIREMENT_FILE", "Retired metadata must be a regular file")


def prepare_root(state_root: Path) -> Path:
    root = retired_root(state_root)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    protected(root, directory=True)
    fsync_directory(root.parent)
    return root


def validate_database(db: AgentDB) -> None:
    if db.metadata("cluster_state") != "DESTROYED" or any(
        not job.state.terminal for job in db.jobs()
    ):
        raise PlatformError("CLUSTER_NOT_DRAINED", "Retired state still contains active work")
    if db.connection.execute("SELECT 1 FROM jobs WHERE retention_finalized=0 LIMIT 1").fetchone():
        raise PlatformError("RETENTION_INCOMPLETE", "Finalize retention before archiving")
