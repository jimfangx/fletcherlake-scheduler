"""Replicate authoritative agent snapshots without inventing hardware execution state."""

from datetime import timedelta
from uuid import UUID, uuid4

from fl_common.errors import PlatformError
from fl_common.models import JobEvent
from fl_common.models.base import utcnow
from fl_common.models.scheduler import ClusterHealth, ClusterSnapshot
from sqlalchemy import select

from ..db.core import Database
from ..db.models import AgentSession, Assignment, Cluster, Event, Job
from .snapshots import apply_snapshot


class Reconciler:
    def __init__(self, db: Database) -> None:
        self.db = db

    def connected(self, cluster_id: UUID) -> UUID:
        session_id = uuid4()
        with self.db.transaction(placement=True) as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is None:
                raise PlatformError("CLUSTER_NOT_FOUND", "Cluster is not registered")
            if cluster.state in {"DESTROYED", "POWERED_OFF", "FORCE_POWER_OFF_PENDING"}:
                raise PlatformError("UNAUTHORIZED_AGENT", "Retired cluster cannot open a session")
            session.add(AgentSession(session_id=session_id, cluster_id=cluster_id))
            cluster.current_session = session_id
        return session_id

    def disconnected(self, cluster_id: UUID, session_id: UUID) -> None:
        with self.db.transaction() as session:
            connection = session.get(AgentSession, session_id)
            if connection:
                connection.disconnected_at = utcnow()
            cluster = session.get(Cluster, cluster_id)
            if cluster and cluster.current_session == session_id:
                cluster.current_session = None
                cluster.health = ClusterHealth.DEGRADED

    def snapshot(self, cluster_id: UUID, session_id: UUID, snapshot: ClusterSnapshot) -> None:
        if snapshot.cluster.cluster_id != cluster_id:
            raise PlatformError(
                "CLUSTER_ID_CONFLICT", "Snapshot differs from authenticated identity"
            )
        with self.db.transaction(placement=True) as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is None or cluster.current_session != session_id:
                raise PlatformError(
                    "STALE_SESSION", "A newer agent connection superseded this session"
                )
            apply_snapshot(session, cluster, snapshot)

    def events(self, cluster_id: UUID, session_id: UUID, events: list[JobEvent]) -> int:
        with self.db.transaction(placement=True) as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is None or cluster.current_session != session_id:
                raise PlatformError("STALE_SESSION", "Agent session is no longer current")
            for event in events:
                if event.seq <= cluster.event_sequence:
                    continue
                if event.seq != cluster.event_sequence + 1:
                    raise PlatformError(
                        "EVENT_SEQUENCE_GAP", "Agent must replay events from last durable cursor"
                    )
                if event.job_id:
                    job = session.get(Job, event.job_id)
                    assignment = session.get(Assignment, event.job_id)
                    if job is None or (assignment and assignment.cluster_id != cluster_id):
                        raise PlatformError(
                            "JOB_PLACEMENT_CONFLICT", "Event job is not part of this cluster"
                        )
                session.add(
                    Event(
                        cluster_id=cluster_id,
                        agent_sequence=event.seq,
                        job_id=event.job_id,
                        type=event.type,
                        timestamp=event.timestamp,
                        payload={**event.payload, "board_id": event.board_id},
                    )
                )
                cluster.event_sequence = event.seq
            return cluster.event_sequence

    def health_sweep(self, degraded_seconds: int = 30, offline_seconds: int = 90) -> None:
        now = utcnow()
        with self.db.transaction(placement=True) as session:
            for cluster in session.scalars(select(Cluster).where(Cluster.state != "DESTROYED")):
                previous = cluster.health
                if cluster.last_heartbeat is None or cluster.last_heartbeat < now - timedelta(
                    seconds=offline_seconds
                ):
                    cluster.health = ClusterHealth.OFFLINE
                elif cluster.current_session is None or cluster.last_heartbeat < now - timedelta(
                    seconds=degraded_seconds
                ):
                    cluster.health = ClusterHealth.DEGRADED
                else:
                    cluster.health = ClusterHealth.ONLINE
                if previous != cluster.health:
                    session.add(
                        Event(
                            cluster_id=cluster.cluster_id,
                            type=f"CLUSTER_{cluster.health}",
                            payload={},
                        )
                    )
                # Connectivity changes never terminate or reassign running hardware jobs.
