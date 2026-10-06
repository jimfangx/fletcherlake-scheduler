"""Ownership-filtered metadata queries. No secret columns are serialized."""

from typing import Any
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models.scheduler import Principal, Role
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.core import Database
from ..db.models import Artifact, Assignment, Board, Cluster, Event, Job, User


def owned_job(session: Session, job_id: UUID, principal: Principal) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise PlatformError("JOB_NOT_FOUND", "Unknown job")
    if not principal.owns(job.owner):
        raise PlatformError("FORBIDDEN", "Job belongs to another user")
    return job


def job_view(session: Session, job: Job) -> dict[str, Any]:
    assignment = session.get(Assignment, job.job_id)
    return {
        "spec": job.spec,
        "state": job.state,
        "record": job.record,
        "error": job.error,
        "updated_at": job.updated_at.isoformat(),
        "cancel_requested": job.cancel_requested,
        "assignment": {
            "cluster_id": str(assignment.cluster_id),
            "board_id": assignment.board_id,
            "state": assignment.state,
            "expires_at": assignment.expires_at.isoformat(),
        }
        if assignment
        else None,
    }


class Queries:
    def __init__(self, db: Database) -> None:
        self.db = db

    def jobs(self, principal: Principal, limit: int, offset: int) -> list[dict[str, Any]]:
        with self.db.transaction() as session:
            query = select(Job).order_by(Job.submitted_at.desc(), Job.job_id)
            if principal.role == Role.USER:
                query = query.where(Job.owner == principal.email)
            return [
                job_view(session, job) for job in session.scalars(query.limit(limit).offset(offset))
            ]

    def job(self, job_id: UUID, principal: Principal) -> dict[str, Any]:
        with self.db.transaction() as session:
            return job_view(session, owned_job(session, job_id, principal))

    def artifacts(self, job_id: UUID, principal: Principal) -> list[dict[str, Any]]:
        with self.db.transaction() as session:
            owned_job(session, job_id, principal)
            return [
                row.metadata_json
                for row in session.scalars(select(Artifact).where(Artifact.job_id == job_id))
            ]

    def events(
        self, job_id: UUID, principal: Principal, after: int, limit: int
    ) -> list[dict[str, Any]]:
        with self.db.transaction() as session:
            owned_job(session, job_id, principal)
            return [
                {
                    "event_id": row.event_id,
                    "type": row.type,
                    "timestamp": row.timestamp.isoformat(),
                    "payload": row.payload,
                }
                for row in session.scalars(
                    select(Event)
                    .where(Event.job_id == job_id, Event.event_id > after)
                    .order_by(Event.event_id)
                    .limit(limit)
                )
            ]

    def clusters(self, principal: Principal) -> list[dict[str, Any]]:
        with self.db.transaction() as session:
            return [
                self._cluster(session, cluster, principal)
                for cluster in session.scalars(select(Cluster).order_by(Cluster.cluster_id))
            ]

    def cluster(self, cluster_id: UUID, principal: Principal) -> dict[str, Any]:
        with self.db.transaction() as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is None:
                raise PlatformError("CLUSTER_NOT_FOUND", "Unknown cluster")
            return self._cluster(session, cluster, principal)

    def _cluster(self, session: Session, cluster: Cluster, principal: Principal) -> dict[str, Any]:
        boards = []
        snapshot = cluster.snapshot or {}
        queues = snapshot.get("queues", {})
        for board in session.scalars(
            select(Board)
            .where(Board.cluster_id == cluster.cluster_id, Board.enabled.is_(True))
            .order_by(Board.board_id)
        ):
            active = session.get(Job, board.active_job) if board.active_job else None
            boards.append(
                {
                    "board_id": board.board_id,
                    "cluster_id": str(cluster.cluster_id),
                    "config": board.config,
                    "state": board.state,
                    "active_job": str(active.job_id)
                    if active and principal.owns(active.owner)
                    else None,
                    "queued_count": len(queues.get(board.board_id, [])) if snapshot else None,
                }
            )
        return {
            "cluster_id": str(cluster.cluster_id),
            "config": cluster.config,
            "state": cluster.state,
            "health": cluster.health,
            "boards": boards,
            "desired_state": cluster.desired_state,
            "last_heartbeat": cluster.last_heartbeat.isoformat()
            if cluster.last_heartbeat
            else None,
            "system": cluster.snapshot.get("system") if cluster.snapshot else None,
            "snapshot_at": snapshot.get("timestamp"),
            "running_count": sum(board["state"] == "RUNNING" for board in boards)
            if snapshot
            else None,
            "queued_count": sum(board["queued_count"] for board in boards) if snapshot else None,
        }

    def board(self, board_id: str, principal: Principal) -> dict[str, Any]:
        with self.db.transaction() as session:
            board = session.get(Board, board_id)
            if board is None or not board.enabled:
                raise PlatformError("BOARD_NOT_FOUND", "Unknown board")
            cluster = session.get(Cluster, board.cluster_id)
            assert cluster is not None
            return next(
                item
                for item in self._cluster(session, cluster, principal)["boards"]
                if item["board_id"] == board_id
            )

    def users(self, principal: Principal) -> list[dict[str, str]]:
        if not principal.permits("system:admin"):
            raise PlatformError("FORBIDDEN", "Administrator role required")
        with self.db.transaction() as session:
            return [
                {
                    "email": user.email,
                    "subject": user.subject,
                    "last_login_at": user.last_login_at.isoformat(),
                }
                for user in session.scalars(select(User).order_by(User.email))
            ]
