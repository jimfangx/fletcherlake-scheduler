"""Serialize user cancellation against transfer/enqueue and durable command creation."""

from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import JobState
from fl_common.models.base import utcnow
from fl_common.models.scheduler import Principal
from fl_common.protocol import Message, MessageType
from sqlalchemy.orm import Session

from ..db.core import Database
from ..db.models import Assignment, Cluster, Command, Event
from .queries import owned_job


def add_command(
    session: Session, cluster_id: UUID, message: Message, job_id: UUID | None = None
) -> None:
    session.add(
        Command(
            message_id=message.message_id,
            cluster_id=cluster_id,
            job_id=job_id,
            envelope=message.model_dump(mode="json"),
        )
    )


class Control:
    def __init__(self, db: Database) -> None:
        self.db = db

    def cancel(self, job_id: UUID, principal: Principal) -> None:
        with self.db.transaction(placement=True) as session:
            job = owned_job(session, job_id, principal)
            if JobState(job.state).terminal or job.cancel_requested:
                return
            job.cancel_requested = True
            job.updated_at = utcnow()
            assignment = session.get(Assignment, job_id)
            if assignment is None or assignment.state in {"RESERVED", "STAGING", "EXPIRED"}:
                # No execution command can be issued after cancellation wins this lock.
                job.state = "CANCELED"
                if assignment:
                    assignment.state = "FINISHED"
                event = "JOB_CANCELED"
            else:
                event = "JOB_CANCEL_REQUESTED"
            session.add(Event(job_id=job_id, type=event, payload={"requested_by": principal.email}))
            if assignment:
                add_command(
                    session,
                    assignment.cluster_id,
                    Message(type=MessageType.JOB_CANCEL, payload={"job_id": str(job_id)}),
                    job_id,
                )

    def delete_artifacts(self, job_id: UUID, principal: Principal) -> UUID:
        with self.db.transaction(placement=True) as session:
            job = owned_job(session, job_id, principal)
            if not JobState(job.state).terminal:
                raise PlatformError("JOB_ACTIVE", "Collateral can only be deleted after completion")
            assignment = session.get(Assignment, job_id)
            if assignment is None:
                raise PlatformError("ARTIFACT_NOT_FOUND", "Job has no agent collateral")
            message = Message(type=MessageType.ARTIFACT_DELETE, payload={"job_id": str(job_id)})
            add_command(session, assignment.cluster_id, message, job_id)
            return message.message_id

    def drain(self, cluster_id: UUID, principal: Principal) -> UUID:
        if not principal.permits("cluster:drain"):
            raise PlatformError("FORBIDDEN", "Operator role required")
        with self.db.transaction(placement=True) as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is None:
                raise PlatformError("CLUSTER_NOT_FOUND", "Unknown cluster")
            if cluster.state in {"DESTROYED", "POWERED_OFF", "FORCE_POWER_OFF_PENDING"}:
                raise PlatformError(
                    "CLUSTER_UNAVAILABLE", "Cluster cannot accept lifecycle commands"
                )
            # Block new global reservations immediately; agent ACK/snapshot confirms physical drain.
            cluster.desired_state = "DRAINING"
            message = Message(type=MessageType.CLUSTER_DRAIN)
            add_command(session, cluster_id, message)
            session.add(
                Event(
                    cluster_id=cluster_id,
                    type="CLUSTER_DRAIN_REQUESTED",
                    payload={"requested_by": principal.email},
                )
            )
            return message.message_id
