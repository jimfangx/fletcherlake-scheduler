"""A fixed-clock snapshot includes active, queued, retained, due, and deleted data."""

from datetime import UTC, datetime, timedelta

from fl_common.models import JobConfig, JobRecord, JobSpec
from fl_common.models.artifact import ArtifactRecord, ArtifactRef
from fl_common.models.scheduler import BoardStatus, ClusterSnapshot, SystemMetrics


def snapshot(config):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    jobs = [
        JobRecord(
            spec=JobSpec.from_config(JobConfig(priority=priority), owner),
            board_id="board-0",
            state=state,
            updated_at=now,
            started_at=now - timedelta(hours=1) if state == "RUNNING" else None,
        )
        for priority, owner, state in ((0, "[bold]jim", "RUNNING"), (-5, "alice", "QUEUED"))
    ]
    artifacts = [
        ArtifactRecord(
            job_id=jobs[0].spec.job_id,
            ref=ArtifactRef(kind=kind, sha256="0" * 64, size_bytes=1024**3),
            expires_at=expiry,
            deleted_at=deleted,
        )
        for kind, expiry, deleted in (
            ("stdout", None, None),
            ("results", now + timedelta(days=29, hours=11), None),
            ("stderr", now, None),
            ("binary", now - timedelta(days=1), now),
        )
    ]
    return ClusterSnapshot(
        cluster=config,
        state="READY",
        timestamp=now,
        boards=[
            BoardStatus(board_id="board-0", state="RUNNING", active_job=jobs[0].spec.job_id),
            BoardStatus(board_id="board-1", state="IDLE"),
        ],
        jobs=jobs,
        queues={"board-0": [jobs[1].spec.job_id], "board-1": []},
        artifacts=artifacts,
        last_event_sequence=1,
        system=SystemMetrics(
            cpu=0.27, memory=0.61, disk_free=577 * 1024**3, disk_total=1000 * 1024**3
        ),
    )
