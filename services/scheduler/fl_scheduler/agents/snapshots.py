"""Apply authoritative local state inside an existing scheduler transaction."""

from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_common.models.scheduler import ClusterHealth, ClusterSnapshot
from sqlalchemy.orm import Session

from ..db.models import Artifact, Assignment, Board, Cluster, Event, Job


def apply_snapshot(session: Session, cluster: Cluster, snapshot: ClusterSnapshot) -> None:
    cluster_id = cluster.cluster_id
    if snapshot.cluster.cluster_id != cluster_id:
        raise PlatformError("CLUSTER_ID_CONFLICT", "Snapshot differs from authenticated identity")
    cluster.last_heartbeat = utcnow()
    if cluster.health != ClusterHealth.ONLINE:
        session.add(Event(cluster_id=cluster_id, type="CLUSTER_ONLINE", payload={}))
    cluster.health = ClusterHealth.ONLINE
    cluster.state = snapshot.state
    if cluster.desired_state == snapshot.state:
        cluster.desired_state = None
    cluster.snapshot = snapshot.model_dump(mode="json")
    for status in snapshot.boards:
        board = session.get(Board, status.board_id)
        if board is None or board.cluster_id != cluster_id:
            raise PlatformError("UNKNOWN_BOARD", "Board is not registered to this cluster")
        board.state = status.state
        board.active_job = status.active_job
    for record in snapshot.jobs:
        board = session.get(Board, record.board_id)
        if board is None or board.cluster_id != cluster_id:
            raise PlatformError("UNKNOWN_BOARD", "Job board does not belong to this cluster")
        job = session.get(Job, record.spec.job_id)
        assignment = session.get(Assignment, record.spec.job_id)
        if assignment and assignment.cluster_id != cluster_id:
            raise PlatformError("JOB_PLACEMENT_CONFLICT", "Job belongs to a different cluster")
        if job is None:
            # Locally submitted jobs enter the replicated inventory via a trusted agent.
            job = Job(
                job_id=record.spec.job_id,
                owner=record.spec.owner,
                spec=record.spec.model_dump(mode="json"),
                priority=record.spec.priority,
                submitted_at=record.spec.submitted_at,
            )
            session.add(job)
            session.flush()
            assignment = Assignment(
                job_id=record.spec.job_id,
                cluster_id=cluster_id,
                board_id=record.board_id,
                state="QUEUED",
                expires_at=utcnow(),
            )
            session.add(assignment)
        elif record.spec.model_dump(mode="json") != job.spec:
            raise PlatformError("JOB_SPEC_CONFLICT", "Agent cannot alter trusted job metadata")
        elif assignment is None:
            raise PlatformError("UNASSIGNED_JOB", "Scheduler job was not assigned to this agent")
        canceled_before_enqueue = (
            job.cancel_requested
            and job.state == "CANCELED"
            and record.state in {"CREATED", "STAGING"}
        )
        if not canceled_before_enqueue:
            job.state = record.state
        job.updated_at = utcnow()
        job.record = record.model_dump(mode="json")
        job.error = record.error
        if assignment:
            if record.state.terminal:
                assignment.state = "FINISHED"
            elif record.state not in {"CREATED", "STAGING"}:
                assignment.state = "QUEUED"
    session.flush()
    for artifact in snapshot.artifacts:
        job = session.get(Job, artifact.job_id)
        if job is None:
            raise PlatformError("JOB_NOT_FOUND", "Artifact has no job in snapshot")
        assignment = session.get(Assignment, artifact.job_id)
        if assignment and assignment.cluster_id != cluster_id:
            raise PlatformError("JOB_PLACEMENT_CONFLICT", "Artifact belongs to another cluster")
        stored = session.get(Artifact, (artifact.job_id, artifact.ref.kind))
        if stored is None:
            stored = Artifact(job_id=artifact.job_id, kind=artifact.ref.kind)
            session.add(stored)
        stored.metadata_json = artifact.model_dump(mode="json")
    seen = set()
    for head in snapshot.logs:
        assignment = session.get(Assignment, head.job_id)
        if assignment is None or assignment.cluster_id != cluster_id:
            raise PlatformError("JOB_PLACEMENT_CONFLICT", "Log belongs to another cluster")
        if (head.job_id, head.stream) in seen:
            raise PlatformError("INVALID_SNAPSHOT", "Duplicate log watermark")
        seen.add((head.job_id, head.stream))
