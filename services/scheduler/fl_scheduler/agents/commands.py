"""Durable outbox commands remain replayable until the agent acknowledges persistence."""

from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import JobState
from fl_common.models.base import utcnow
from fl_common.protocol import Ack, Message, MessageType
from sqlalchemy import select

from ..db.core import Database
from ..db.models import Assignment, Cluster, Command, Event, Job
from ..transfers.models import Delivery
from .outbox import pending_commands


class Commands:
    def __init__(self, db: Database) -> None:
        self.db = db

    def enqueue_empty(self) -> None:
        """Jobs declaring no input bytes need no upload gate, including after restart."""
        with self.db.transaction() as session:
            jobs = list(
                session.scalars(
                    select(Job.job_id)
                    .join(Assignment)
                    .where(
                        Assignment.state.in_(["RESERVED", "STAGING"]),
                        Assignment.expires_at > utcnow(),
                        Job.cancel_requested.is_(False),
                        Job.spec["binary"].as_string().is_(None),
                        Job.spec["bitstream"].as_string().is_(None),
                    )
                    .limit(100)
                )
            )
        for job_id in jobs:
            try:
                self.enqueue_after_transfer(job_id)
            except PlatformError as error:
                if error.code not in {"JOB_CANCELED", "RESERVATION_EXPIRED"}:
                    raise

    def enqueue_after_transfer(self, job_id: UUID) -> Message:
        """Called only by the verified transfer path, never by an untrusted client directly."""
        with self.db.transaction(placement=True) as session:
            assignment = session.get(Assignment, job_id)
            job = session.get(Job, job_id)
            if assignment is None or job is None:
                raise PlatformError("JOB_NOT_FOUND", "Job has no assignment")
            if job.cancel_requested:
                raise PlatformError("JOB_CANCELED", "Job cancellation prevents enqueue")
            existing = session.scalars(
                select(Command)
                .where(
                    Command.job_id == job_id,
                )
                .order_by(Command.created_at)
            ).all()
            for command in existing:
                message = Message.model_validate(command.envelope)
                if message.type == MessageType.JOB_ENQUEUE:
                    return message
            delivery = session.get(Delivery, job_id)
            verified_delivery = (
                assignment.state == "DELIVERING"
                and delivery is not None
                and delivery.state == "READY"
                and delivery.deadline > utcnow()
            )
            active_reservation = (
                assignment.state in {"RESERVED", "STAGING"} and assignment.expires_at > utcnow()
            )
            if not (verified_delivery or active_reservation):
                raise PlatformError(
                    "RESERVATION_EXPIRED", "Transfer reservation is no longer active"
                )
            message = Message(
                type=MessageType.JOB_ENQUEUE,
                payload={
                    "spec": job.spec,
                    "board_id": assignment.board_id,
                },
            )
            assignment.state = "DELIVERING"
            if delivery:
                delivery.state = "ENQUEUING"
            session.add(
                Command(
                    message_id=message.message_id,
                    cluster_id=assignment.cluster_id,
                    job_id=job_id,
                    envelope=message.model_dump(mode="json"),
                )
            )
            return message

    def issue(self, cluster_id: UUID, message: Message, job_id: UUID | None = None) -> Message:
        with self.db.transaction() as session:
            if session.get(Command, message.message_id) is None:
                session.add(
                    Command(
                        message_id=message.message_id,
                        cluster_id=cluster_id,
                        job_id=job_id,
                        envelope=message.model_dump(mode="json"),
                    )
                )
        return message

    def pending(self, cluster_id: UUID, limit: int | None = None) -> list[Message]:
        return pending_commands(self.db, cluster_id, limit)

    def acknowledge(self, cluster_id: UUID, ack: Ack, session_id: UUID | None = None) -> None:
        with self.db.transaction(placement=True) as session:
            if session_id is not None:
                cluster = session.get(Cluster, cluster_id)
                if cluster is None or cluster.current_session != session_id:
                    raise PlatformError(
                        "STALE_AGENT_SESSION", "ACK belongs to a superseded connection"
                    )
            command = session.get(Command, ack.message_id)
            if command is None or command.cluster_id != cluster_id:
                raise PlatformError("COMMAND_NOT_FOUND", "ACK does not belong to this agent")
            if command.acknowledged_at is not None:
                return
            command.acknowledged_at = utcnow()
            command.response = ack.model_dump(mode="json")
            message = Message.model_validate(command.envelope)
            if command.job_id and message.type == MessageType.JOB_ENQUEUE:
                job = session.get(Job, command.job_id)
                assignment = session.get(Assignment, command.job_id)
                assert job is not None and assignment is not None
                if JobState(job.state).terminal:
                    assignment.state = "FINISHED"
                    return
                if not ack.accepted:
                    job.state = "FAILED"
                    job.error = ack.error
                    assignment.state = "FINISHED"
                    session.add(
                        Event(job_id=job.job_id, type="JOB_FAILED", payload={"error": ack.error})
                    )
                elif job.state in {"STAGING", "CREATED"}:
                    job.state = "QUEUED"
                    assignment.state = "QUEUED"
                    session.add(Event(job_id=job.job_id, type="JOB_QUEUED", payload={}))
