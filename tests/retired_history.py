"""Real SQLite terminal jobs and protected retirement intent for filesystem recovery tests."""

from pathlib import Path
from uuid import uuid4

from fl_agent.collateral import CollateralStore
from fl_agent.configuration import write_config
from fl_agent.db import AgentDB
from fl_agent.retired_state import prepare_root
from fl_agent.retirement import RetiredInstallation, Retirement, archive_state, finish
from fl_common.locks import ExclusiveLock
from fl_common.models import JobConfig, JobSpec
from fl_common.models.base import utcnow
from fl_common.models.scheduler import ClusterSnapshot


def history(state: Path, config, *, archive: bool = True) -> Retirement:
    config = config.model_copy(deep=True)
    config.cluster_id = config.cluster_id or uuid4()
    write_config(state, config)
    state.chmod(0o700)
    db = AgentDB(state / "agent.db")
    try:
        db.initialize_boards(["board-0"])
        spec = JobSpec.from_config(JobConfig(run_collateral_ttl=1), "alice")
        db.create(spec, "board-0")
        store = CollateralStore(state / "jobs", db)
        store.root.chmod(0o700)
        store.save_spec(spec)
        final = db.cancel(spec.job_id)
        store.path(spec.job_id, "results").write_text("retained result")
        store.finalize(spec, final.finished_at)
        db.set_metadata("cluster_state", "DESTROYED")
        snapshot = ClusterSnapshot(
            cluster=config,
            state="DESTROYED",
            timestamp=utcnow(),
            boards=db.boards(),
            jobs=db.jobs(),
            queues={},
            artifacts=store.records(),
            last_event_sequence=db.last_sequence(),
            system={"cpu": 0, "memory": 0, "disk_free": 1, "disk_total": 1},
        )
    finally:
        db.close()
    (state / "agent.db").chmod(0o600)
    receipt = Retirement(
        state_root=state,
        runtime_root=state.parent / "run",
        snapshot=snapshot,
        installation=RetiredInstallation(project=state.parent, pixi="/opt/pixi/bin/pixi"),
        phase="LOGGED_OUT",
    )
    root = prepare_root(state)
    receipt.save(root)
    if archive:
        with (
            ExclusiveLock(state / "agent.lock"),
            ExclusiveLock(receipt.runtime_root / "agent.lock"),
        ):
            archive_state(root, receipt)
        finish(root)
    return receipt
