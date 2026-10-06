"""Retired cluster storage. Expiry remains authoritative in each archived SQLite database."""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.locks import ExclusiveLock
from fl_common.models.scheduler import ClusterSnapshot

from .collateral import CollateralStore
from .db import AgentDB
from .retired_state import protected, validate_database
from .retirement import Retirement, archive_state, finish

logger = logging.getLogger(__name__)


@dataclass
class SweepReport:
    deleted: int = 0
    failed: int = 0


def sweep_retired(root: Path, *, now: datetime | None = None) -> SweepReport:
    report = SweepReport()
    if not root.exists():
        return report
    protected(root, directory=True)
    try:
        report.deleted += sweep_pending(root, now)
    except PlatformError as error:
        if error.code != "ALREADY_OWNED":
            report.failed += 1
            logger.error("Pending retirement: %s", error.code)
    except Exception:
        report.failed += 1
        logger.error("Pending retirement: cleanup failed; will retry")
    for archive in sorted(root.iterdir()):
        try:
            identity = UUID(archive.name)
        except ValueError:
            continue
        try:
            protected(archive, directory=True)
            protected(archive / "snapshot.json")
            snapshot = ClusterSnapshot.model_validate_json((archive / "snapshot.json").read_bytes())
            if snapshot.cluster.cluster_id != identity or snapshot.state != "DESTROYED":
                raise PlatformError(
                    "ARCHIVE_IDENTITY", "Archive identity differs from final snapshot"
                )
            protected(archive / "agent.db")
            protected(archive / "jobs", directory=True)
            with ExclusiveLock(archive / "agent.lock"):
                db = AgentDB(archive / "agent.db")
                try:
                    validate_database(db)
                    report.deleted += CollateralStore(archive / "jobs", db).sweep(now)
                    db.checkpoint()
                finally:
                    db.close()
        except PlatformError as error:
            if error.code != "ALREADY_OWNED":
                report.failed += 1
                logger.error("Retired cluster %s: %s", identity, error.code)
        except Exception:
            report.failed += 1
            # No collateral contents, configuration or credentials enter maintenance logs.
            logger.error("Retired cluster %s: cleanup failed; will retry", identity)
    return report


def sweep_pending(root: Path, now: datetime | None) -> int:
    if not (root / "pending-destroy.json").exists():
        return 0
    with ExclusiveLock(root / "management.lock"):
        receipt = Retirement.load(root)
        if receipt is None:
            return 0
        target = receipt.archive(root)
        if target.exists() and not receipt.state_root.exists():
            protected(target / "snapshot.json")
            final = ClusterSnapshot.model_validate_json((target / "snapshot.json").read_bytes())
            if final != receipt.snapshot:
                raise PlatformError("ARCHIVE_CONFLICT", "Archived snapshot differs from intent")
            finish(root)
            return 0
        protected(receipt.state_root, directory=True)
        with (
            ExclusiveLock(receipt.state_root / "agent.lock"),
            ExclusiveLock(receipt.runtime_root / "agent.lock"),
        ):
            if receipt.phase == "LOGGED_OUT":
                archive_state(root, receipt)
                finish(root)
                return 0  # The regular archive loop sweeps this freshly published directory.
            database = receipt.state_root / "agent.db"
            if database.is_symlink() or not database.is_file():
                raise PlatformError("RETIREMENT_DATABASE", "Pending history is absent or unsafe")
            protected(receipt.state_root / "jobs", directory=True)
            db = AgentDB(database)
            try:
                validate_database(db)
                return CollateralStore(receipt.state_root / "jobs", db).sweep(now)
            finally:
                db.close()
