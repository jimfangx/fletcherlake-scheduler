"""Short delivery transactions serialize cancellation, retries, and outbox creation."""

from datetime import timedelta
from uuid import UUID, uuid4

from fl_common.errors import PlatformError
from fl_common.models import JobSpec, JobState
from fl_common.models.base import utcnow
from fl_common.models.scheduler import Principal
from fl_common.models.transfer import TransferGrant
from fl_common.protocol import Ack, Message, MessageType
from fl_common.protocol.delivery import FetchCommand, StageReceipt
from sqlalchemy import select

from ..api.control import add_command
from ..api.queries import owned_job
from ..db.core import Database
from ..db.models import Assignment, Command, Event, Job
from .models import Delivery
from .queries import DeliveryQueries, latest


class DeliveryState(DeliveryQueries):
    def __init__(self, db: Database) -> None:
        self.db = db

    def authorize(self, job_id: UUID, principal: Principal) -> None:
        with self.db.transaction() as session:
            owned_job(session, job_id, principal)

    def begin(self, job_id: UUID, principal: Principal, source: TransferGrant) -> UUID:
        with self.db.transaction(placement=True) as session:
            job = owned_job(session, job_id, principal)
            assignment = session.get(Assignment, job_id)
            existing = session.get(Delivery, job_id)
            if existing:
                if TransferGrant.model_validate(existing.source).model_dump(
                    exclude={"expires_at"}
                ) != source.model_dump(exclude={"expires_at"}):
                    raise PlatformError(
                        "TRANSFER_ID_CONFLICT", "Delivery source differs from prior request"
                    )
                return existing.transfer_id
            if job.cancel_requested or JobState(job.state).terminal:
                raise PlatformError("JOB_CANCELED", "Job no longer accepts delivery")
            if (
                assignment is None
                or assignment.state not in {"RESERVED", "STAGING"}
                or assignment.expires_at <= utcnow()
            ):
                raise PlatformError(
                    "RESERVATION_EXPIRED", "Verified delivery requires an active reservation"
                )
            spec = JobSpec.model_validate(job.spec)
            expected = {ref.kind: ref for ref in (spec.binary, spec.bitstream) if ref}
            if (
                source.direction != "upload"
                or source.job_id != job_id
                or {ref.kind: ref for ref in source.files} != expected
            ):
                raise PlatformError(
                    "TRANSFER_SCOPE", "Verified source differs from trusted job inputs"
                )
            transfer_id = uuid4()
            deadline = min(utcnow() + timedelta(minutes=10), source.retains_until)
            if deadline <= utcnow():
                raise PlatformError("TRANSFER_EXPIRED", "Source payload retention expired")
            session.add(
                Delivery(
                    job_id=job_id,
                    transfer_id=transfer_id,
                    source=source.model_dump(mode="json"),
                    deadline=deadline,
                )
            )
            assignment.state = "DELIVERING"
            add_command(
                session,
                assignment.cluster_id,
                Message(
                    type=MessageType.JOB_STAGE,
                    payload={
                        "spec": job.spec,
                        "board_id": assignment.board_id,
                        "transfer_id": str(transfer_id),
                    },
                ),
                job_id,
            )
            session.add(Event(job_id=job_id, type="JOB_DELIVERY_STARTED", payload={}))
            return transfer_id

    def fetch(self, command: FetchCommand) -> None:
        with self.db.transaction(placement=True) as session:
            row, job = session.get(Delivery, command.job_id), session.get(Job, command.job_id)
            assignment = session.get(Assignment, command.job_id)
            assert row is not None and job is not None and assignment is not None
            if (
                job.cancel_requested
                or JobState(job.state).terminal
                or row.deadline <= utcnow()
                or row.next_attempt_at > utcnow()
            ):
                return
            commands = list(
                session.scalars(
                    select(Command)
                    .where(Command.job_id == command.job_id)
                    .order_by(Command.created_at, Command.message_id)
                )
            )
            previous = latest(commands, MessageType.JOB_FETCH)
            if previous and (
                previous.acknowledged_at is None or Ack.model_validate(previous.response).accepted
            ):
                return
            stage = latest(commands, MessageType.JOB_STAGE)
            if stage is None or stage.response is None:
                raise PlatformError("TRANSFER_STAGE", "Mac staging is not acknowledged")
            receipt = StageReceipt.model_validate(Ack.model_validate(stage.response).result)
            if (
                receipt.transfer_id != command.grant.transfer_id
                or receipt.public_key != command.grant.public_key
            ):
                raise PlatformError(
                    "TRANSFER_IDENTITY", "Fetch differs from acknowledged Mac identity"
                )
            add_command(
                session,
                assignment.cluster_id,
                Message(type=MessageType.JOB_FETCH, payload=command.model_dump(mode="json")),
                command.job_id,
            )
            row.state, row.attempts = "FETCHING", row.attempts + 1
            row.next_attempt_at = utcnow() + timedelta(seconds=min(60, 2 ** min(row.attempts, 5)))

    def ready(self, job_id: UUID) -> None:
        with self.db.transaction(placement=True) as session:
            row = session.get(Delivery, job_id)
            assert row is not None
            if row.state in {"STAGING", "FETCHING"}:
                row.state = "READY"

    def cancel(self, job_id: UUID, reason: str) -> None:
        with self.db.transaction(placement=True) as session:
            row, job = session.get(Delivery, job_id), session.get(Job, job_id)
            assignment = session.get(Assignment, job_id)
            assert row is not None and job is not None and assignment is not None
            if row.state == "CANCELING" or JobState(job.state).terminal:
                return
            if not job.cancel_requested:
                job.cancel_requested = True
                add_command(
                    session,
                    assignment.cluster_id,
                    Message(type=MessageType.JOB_CANCEL, payload={"job_id": str(job_id)}),
                    job_id,
                )
            row.state, row.error = "CANCELING", reason
            session.add(
                Event(job_id=job_id, type="JOB_DELIVERY_FAILED", payload={"reason": reason})
            )

    def done(self, job_id: UUID) -> None:
        with self.db.transaction(placement=True) as session:
            row = session.get(Delivery, job_id)
            assert row is not None
            row.state = "DONE"

    def defer(self, job_id: UUID) -> None:
        """Advance the scan cursor so an old batch cannot starve later deliveries."""
        with self.db.transaction(placement=True) as session:
            row = session.get(Delivery, job_id)
            if row:
                row.next_attempt_at = max(row.next_attempt_at, utcnow() + timedelta(seconds=2))
