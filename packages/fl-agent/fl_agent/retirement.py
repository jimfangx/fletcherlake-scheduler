"""Retirement journal and atomic filesystem archival; no hardware or network dependencies."""

from pathlib import Path
from typing import Literal
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.files import atomic_write, fsync_directory
from fl_common.models.base import Schema
from fl_common.models.scheduler import ClusterSnapshot

from .db import AgentDB
from .retired_state import protected, retired_root, validate_database


class RetiredInstallation(Schema):
    project: Path
    pixi: str


class CompletedRetirement(Schema):
    cluster_id: UUID


def completed_snapshot(root: Path) -> ClusterSnapshot | None:
    latest = root / "latest.json"
    if not latest.exists():
        return None
    protected(latest)
    identity = CompletedRetirement.model_validate_json(latest.read_bytes()).cluster_id
    target = root / str(identity)
    protected(target, directory=True)
    protected(target / "snapshot.json")
    snapshot = ClusterSnapshot.model_validate_json((target / "snapshot.json").read_bytes())
    if snapshot.cluster.cluster_id != identity or snapshot.state != "DESTROYED":
        raise PlatformError("ARCHIVE_IDENTITY", "Completed retirement has another cluster identity")
    return snapshot


class Retirement(Schema):
    state_root: Path
    runtime_root: Path
    snapshot: ClusterSnapshot
    installation: RetiredInstallation
    phase: Literal["DRAINED", "UNREGISTERED", "LOGGED_OUT"] = "DRAINED"

    def save(self, root: Path) -> None:
        atomic_write(root / "pending-destroy.json", self.model_dump_json().encode())

    @classmethod
    def load(cls, root: Path) -> "Retirement | None":
        path = root / "pending-destroy.json"
        if not path.exists():
            return None
        protected(path)
        receipt = cls.model_validate_json(path.read_bytes())
        if (
            not receipt.state_root.is_absolute()
            or not receipt.runtime_root.is_absolute()
            or (retired_root(receipt.state_root) != root)
        ):
            raise PlatformError(
                "ARCHIVE_IDENTITY", "Retirement intent belongs to another state root"
            )
        return receipt

    def archive(self, root: Path) -> Path:
        identity = self.snapshot.cluster.cluster_id
        if identity is None:
            raise PlatformError("CLUSTER_ID_CONFLICT", "Retiring a cluster requires a stable UUID")
        return root / str(identity)


def archive_state(root: Path, receipt: Retirement) -> None:
    source, target = receipt.state_root, receipt.archive(root)
    if receipt.phase != "LOGGED_OUT":
        raise PlatformError("DESTROY_PENDING", "Native teardown must finish before archival")
    if target.exists():
        raise PlatformError("ARCHIVE_CONFLICT", "Cluster history already has an archive")
    protected(source, directory=True)
    database = source / "agent.db"
    if database.is_symlink() or not database.is_file():
        raise PlatformError("RETIREMENT_DATABASE", "Agent history is absent or unsafe")
    db = AgentDB(database)
    try:
        validate_database(db)
        # Caller holds both daemon locks, preventing writes after this checkpoint.
        result = db.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result[0]:
            raise PlatformError("RETIREMENT_DATABASE", "Agent history is still in use")
    finally:
        db.close()
    database.chmod(0o600)
    jobs = source / "jobs"
    if jobs.is_symlink() or not jobs.is_dir():
        raise PlatformError("RETIREMENT_DIRECTORY", "Collateral directory is absent or unsafe")
    jobs.chmod(0o700)
    atomic_write(source / "snapshot.json", receipt.snapshot.model_dump_json().encode())
    for name in ("cluster.sha256", "credentials.json", "enrollment.json", "headscale-join.key"):
        (source / name).unlink(missing_ok=True)
    fsync_directory(source)
    source.rename(target)
    fsync_directory(source.parent)
    fsync_directory(root)


def finish(root: Path) -> None:
    receipt = Retirement.load(root)
    if receipt is None:
        raise PlatformError("DESTROY_PENDING", "No retirement intent is available to complete")
    target = receipt.archive(root)
    protected(target / "snapshot.json")
    saved = ClusterSnapshot.model_validate_json((target / "snapshot.json").read_bytes())
    if saved != receipt.snapshot:
        raise PlatformError("ARCHIVE_CONFLICT", "Archived snapshot differs from retirement intent")
    completed = CompletedRetirement(cluster_id=UUID(target.name))
    atomic_write(root / "latest.json", completed.model_dump_json().encode())
    (root / "pending-destroy.json").unlink()
    fsync_directory(root)
