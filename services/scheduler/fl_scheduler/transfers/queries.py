"""Read delivery work and completed outbox receipts without owning network operations."""

from dataclasses import dataclass
from uuid import UUID

from fl_common.models import JobSpec, JobState
from fl_common.models.base import utcnow
from fl_common.protocol import Ack, MessageType
from sqlalchemy import select

from ..db.core import Database
from ..db.models import Assignment, Cluster, Command, Job
from .models import Delivery


@dataclass(frozen=True)
class Work:
    delivery: Delivery
    spec: JobSpec
    assignment: Assignment
    canceled: bool
    terminal: bool
    networks: tuple[str, ...]
    stage: Ack | None
    fetch: Ack | None
    fetching: bool


def latest(commands: list[Command], kind: MessageType) -> Command | None:
    return next(
        (command for command in reversed(commands) if command.envelope["type"] == kind), None
    )


class DeliveryQueries:
    def __init__(self, db: Database) -> None:
        self.db = db

    def pending(self) -> list[UUID]:
        with self.db.transaction() as session:
            return list(
                session.scalars(
                    select(Delivery.job_id)
                    .where(Delivery.state != "DONE")
                    .where(Delivery.next_attempt_at <= utcnow())
                    .order_by(Delivery.next_attempt_at)
                    .limit(100)
                )
            )

    def work(self, job_id: UUID) -> Work:
        with self.db.transaction(placement=True) as session:
            row, job = session.get(Delivery, job_id), session.get(Job, job_id)
            assignment = session.get(Assignment, job_id)
            assert row is not None and job is not None and assignment is not None
            cluster = session.get(Cluster, assignment.cluster_id)
            assert cluster is not None
            commands = list(
                session.scalars(
                    select(Command)
                    .where(Command.job_id == job_id)
                    .order_by(Command.created_at, Command.message_id)
                )
            )
            stage, fetch = (
                latest(commands, MessageType.JOB_STAGE),
                latest(commands, MessageType.JOB_FETCH),
            )
            networks = tuple(
                f"{address}/{'128' if ':' in address else '32'}"
                for address in (cluster.headscale_addresses or [])
            )
            return Work(
                row,
                JobSpec.model_validate(job.spec),
                assignment,
                job.cancel_requested,
                JobState(job.state).terminal,
                networks,
                Ack.model_validate(stage.response) if stage and stage.response else None,
                Ack.model_validate(fetch.response) if fetch and fetch.response else None,
                fetch is not None and fetch.acknowledged_at is None,
            )
