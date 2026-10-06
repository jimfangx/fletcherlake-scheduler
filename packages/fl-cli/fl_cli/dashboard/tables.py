"""Snapshot projections preserve queue order and keep physical history visible."""

from dataclasses import dataclass

from fl_common.models.scheduler import ClusterSnapshot

from .formatting import duration, expiry, size


@dataclass(frozen=True)
class Row:
    key: str
    cells: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    rows: tuple[Row, ...]


def tables(snapshot: ClusterSnapshot) -> dict[str, Table]:
    inventory = {board.board_id: board for board in snapshot.cluster.boards if board}
    jobs = {job.spec.job_id: job for job in snapshot.jobs}
    boards, job_rows, queues, artifacts = [], [], [], []
    for board in snapshot.boards:
        config = inventory.get(board.board_id)
        boards.append(
            Row(
                board.board_id,
                (
                    board.board_id,
                    (", ".join(soc.name for soc in config.socs) or "—") if config else "—",
                    (", ".join(fpga.model for fpga in config.fpgas) or "—") if config else "—",
                    board.state,
                    str(board.active_job) if board.active_job else "—",
                    str(len(snapshot.queues.get(board.board_id, []))),
                    board.error or "—",
                ),
            )
        )
    for job in snapshot.jobs:
        end = job.finished_at or snapshot.timestamp
        elapsed = duration((end - job.started_at).total_seconds()) if job.started_at else "—"
        job_rows.append(
            Row(
                str(job.spec.job_id),
                (
                    job.spec.owner,
                    str(job.spec.job_id),
                    job.board_id,
                    elapsed,
                    str(job.state),
                    str(job.spec.priority),
                ),
            )
        )
    for board_id, queued in snapshot.queues.items():
        for position, job_id in enumerate(queued, 1):
            queued_job = jobs.get(job_id)
            queues.append(
                Row(
                    f"{board_id}:{job_id}",
                    (
                        board_id,
                        str(position),
                        str(job_id),
                        queued_job.spec.owner if queued_job else "—",
                        str(queued_job.spec.priority) if queued_job else "—",
                    ),
                )
            )
    for record in snapshot.artifacts:
        owner_job = jobs.get(record.job_id)
        artifacts.append(
            Row(
                f"{record.job_id}:{record.ref.kind}",
                (
                    owner_job.spec.owner if owner_job else "—",
                    str(record.job_id),
                    record.ref.kind,
                    size(record.ref.size_bytes),
                    "Deleted"
                    if record.deleted_at
                    else expiry(record.expires_at, snapshot.timestamp),
                ),
            )
        )
    return {
        "boards": Table(("Board", "SoC", "FPGA", "State", "Job", "Queued", "Error"), tuple(boards)),
        "jobs": Table(("Owner", "Job", "Board", "Elapsed", "State", "Priority"), tuple(job_rows)),
        "queues": Table(("Board", "Position", "Job", "Owner", "Priority"), tuple(queues)),
        "artifacts": Table(("Owner", "Job", "Kind", "Size", "Deletes in"), tuple(artifacts)),
    }
