"""Finish undelivered reservations and transfer only acknowledged retired board ownership."""

from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_common.models.scheduler import ClusterSnapshot
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Assignment, Board, Cluster, Event, Job


def finish_unseen_assignments(
    session: Session, cluster_id: UUID, snapshot: ClusterSnapshot
) -> None:
    seen = {job.spec.job_id for job in snapshot.jobs}
    for assignment in session.scalars(
        select(Assignment).where(Assignment.cluster_id == cluster_id)
    ):
        if assignment.state == "FINISHED" or assignment.job_id in seen:
            continue
        job = session.get(Job, assignment.job_id)
        if (
            job is None
            or job.state not in {"CREATED", "STAGING"}
            or assignment.state
            not in {
                "RESERVED",
                "STAGING",
                "DELIVERING",
                "EXPIRED",
            }
        ):
            raise PlatformError("CLUSTER_NOT_DRAINED", "Final snapshot omits a dispatched job")
        # A durable full local snapshot has no record of this not-yet-enqueued reservation.
        job.state, job.cancel_requested, job.updated_at = "CANCELED", True, utcnow()
        assignment.state = "FINISHED"
        session.add(
            Event(
                cluster_id=cluster_id,
                job_id=job.job_id,
                type="JOB_CANCELED",
                payload={"reason": "CLUSTER_UNREGISTERED"},
            )
        )


def transfer_retired_board(session: Session, board: Board, new_cluster_id: UUID) -> None:
    previous = session.get(Cluster, board.cluster_id)
    acknowledged = session.scalar(
        select(Event.event_id)
        .where(
            Event.cluster_id == board.cluster_id,
            Event.type == "CLUSTER_UNREGISTERED",
        )
        .limit(1)
    )
    unfinished = session.scalar(
        select(Assignment.job_id)
        .where(
            Assignment.board_id == board.board_id,
            Assignment.state != "FINISHED",
        )
        .limit(1)
    )
    if (
        previous is None
        or previous.state != "DESTROYED"
        or acknowledged is None
        or (board.enabled or board.active_job is not None or unfinished is not None)
    ):
        raise PlatformError(
            "BOARD_ID_CONFLICT", f"Board {board.board_id} belongs to another cluster"
        )
    former = board.cluster_id
    board.cluster_id, board.state, board.active_job = new_cluster_id, "IDLE", None
    session.add(
        Event(
            cluster_id=new_cluster_id,
            type="BOARD_REASSIGNED",
            payload={"board_id": board.board_id, "previous_cluster_id": str(former)},
        )
    )
