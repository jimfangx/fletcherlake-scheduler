"""Transactional pre-transfer reservations and niceness/FIFO scheduling."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import ArtifactRef, BoardConfig, JobConfig, JobSpec, JobState
from fl_common.models.base import utcnow
from fl_common.models.scheduler import AssignmentState, JobAssignment, Principal
from fl_common.scheduling import QueuePolicy, matches
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.core import Database
from ..db.models import Assignment, Board, Cluster, Event, Job
from .creation import JobCreation


@dataclass(frozen=True)
class PlacementSettings:
    reservation_seconds: int = 600
    heartbeat_max_age: int = 30
    queue: QueuePolicy = QueuePolicy()


class Placement:
    def __init__(self, db: Database, settings: PlacementSettings | None = None) -> None:
        self.db = db
        self.settings = settings or PlacementSettings()

    def submit(
        self,
        config: JobConfig,
        principal: Principal,
        *,
        binary: ArtifactRef | None = None,
        bitstream: ArtifactRef | None = None,
    ) -> JobSpec:
        return JobCreation(self.db).submit(config, principal, binary=binary, bitstream=bitstream)

    def reserve_pending(self) -> list[JobAssignment]:
        reservations: list[JobAssignment] = []
        with self.db.transaction(placement=True) as session:
            self._expire(session)
            pending = session.scalars(
                select(Job)
                .where(Job.state == "CREATED", Job.cancel_requested.is_(False))
                .order_by(
                    Job.priority,
                    Job.submitted_at,
                    Job.job_id,
                )
            ).all()
            for job in pending:
                reservation = self._reserve(session, job)
                if reservation:
                    reservations.append(reservation)
        return reservations

    def _reserve(self, session: Session, job: Job) -> JobAssignment | None:
        spec = JobSpec.model_validate(job.spec)
        live = utcnow() - timedelta(seconds=self.settings.heartbeat_max_age)
        boards = session.scalars(
            select(Board)
            .join(Cluster)
            .where(
                Board.enabled.is_(True),
                Board.state != "ERROR",
                Cluster.state == "READY",
                Cluster.desired_state.is_(None),
                Cluster.health == "ONLINE",
                Cluster.last_heartbeat >= live,
            )
        ).all()
        reserved = set(
            session.scalars(
                select(Assignment.board_id).where(
                    Assignment.state.in_([AssignmentState.RESERVED, AssignmentState.STAGING]),
                )
            )
        )
        eligible = [
            board
            for board in boards
            if board.board_id not in reserved
            and matches(
                BoardConfig.model_validate(board.config),
                spec.resource_constraints,
            )
        ]
        if not eligible:
            return None
        board = min(eligible, key=lambda board: (self._cost(session, board), board.board_id))
        expires = utcnow() + timedelta(seconds=self.settings.reservation_seconds)
        assignment = session.get(Assignment, job.job_id)
        if assignment is None:
            assignment = Assignment(job_id=job.job_id)
            session.add(assignment)
        assignment.cluster_id = board.cluster_id
        assignment.board_id = board.board_id
        assignment.state = AssignmentState.RESERVED
        assignment.expires_at = expires
        job.state = "STAGING"
        job.updated_at = utcnow()
        session.add(
            Event(
                job_id=job.job_id,
                cluster_id=board.cluster_id,
                type="JOB_RESERVED",
                payload={"board_id": board.board_id, "expires_at": expires.isoformat()},
            )
        )
        session.flush()
        return JobAssignment(
            job_id=job.job_id,
            cluster_id=board.cluster_id,
            board_id=board.board_id,
            state=AssignmentState.RESERVED,
            expires_at=expires,
        )

    def _cost(self, session: Session, board: Board) -> float:
        assigned = session.scalars(
            select(Job)
            .join(Assignment)
            .where(
                Assignment.board_id == board.board_id,
                Assignment.state.in_(["DELIVERING", "QUEUED"]),
            )
        ).all()
        queued: list[float] = []
        running = 0.0
        for job in assigned:
            spec = JobSpec.model_validate(job.spec)
            if job.state == "QUEUED" or job.state == "STAGING":
                queued.append(spec.run_timeout_seconds)
            elif not JobState(job.state).terminal:
                elapsed = 0.0
                if job.record and job.record.get("started_at"):
                    from fl_common.models import JobRecord

                    record = JobRecord.model_validate(job.record)
                    if record.started_at:
                        elapsed = (utcnow() - record.started_at).total_seconds()
                running += max(0, spec.run_timeout_seconds - elapsed)
        return self.settings.queue.score(queued, running)

    def _expire(self, session: Session) -> None:
        now = utcnow()
        expired = session.scalars(
            select(Assignment).where(
                Assignment.state.in_(["RESERVED", "STAGING"]),
                Assignment.expires_at <= now,
            )
        ).all()
        for assignment in expired:
            assignment.state = AssignmentState.EXPIRED
            job = session.get(Job, assignment.job_id)
            assert job is not None
            job.state = "CREATED"
            job.updated_at = now
            session.add(Event(job_id=job.job_id, type="RESERVATION_EXPIRED", payload={}))

    def begin_staging(self, job_id: UUID, principal: Principal) -> JobAssignment:
        with self.db.transaction(placement=True) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise PlatformError("JOB_NOT_FOUND", "Unknown job")
            if not principal.owns(job.owner):
                raise PlatformError("FORBIDDEN", "Job belongs to another user")
            assignment = session.get(Assignment, job_id)
            if assignment is None or assignment.state not in {"RESERVED", "STAGING"}:
                raise PlatformError(
                    "RESERVATION_UNAVAILABLE", "Job has no active transfer reservation"
                )
            if assignment.expires_at <= utcnow():
                raise PlatformError("RESERVATION_EXPIRED", "Transfer reservation expired")
            assignment.state = AssignmentState.STAGING
            return JobAssignment(
                job_id=job_id,
                cluster_id=assignment.cluster_id,
                board_id=assignment.board_id,
                state=AssignmentState.STAGING,
                expires_at=assignment.expires_at,
            )
