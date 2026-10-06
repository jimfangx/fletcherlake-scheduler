"""Administrator-issued bootstrap and agent-initiated unregister operations."""

import asyncio
import secrets
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import ClusterConfig
from fl_common.models.enrollment import EnrollmentGrant, EnrollmentIssued, EnrollmentRegistration
from fl_common.models.scheduler import ClusterSnapshot, Principal
from pydantic import SecretStr
from sqlalchemy import select

from ..agents.snapshots import apply_snapshot
from ..auth.sessions import digest
from ..db.core import Database
from ..db.models import Board, Cluster, Event
from ..registry import Registry
from .headscale import NetworkControl, NetworkNode
from .retirement import finish_unseen_assignments
from .tickets import Tickets, checked_ticket, queue_revocation


class EnrollmentService:
    def __init__(
        self,
        db: Database,
        network: NetworkControl,
        login_url: str,
        encryption_key: SecretStr,
        *,
        agent_origin: str,
    ) -> None:
        self.db, self.network = db, network
        self.tickets = Tickets(db, encryption_key, login_url, agent_origin)

    @staticmethod
    def _admin(principal: Principal) -> None:
        if not principal.permits("cluster:enroll"):
            raise PlatformError("FORBIDDEN", "Administrator role required")

    async def issue(self, principal: Principal, lifetime: int = 1800) -> EnrollmentIssued:
        self._admin(principal)
        return await asyncio.to_thread(self.tickets.issue, principal.subject, lifetime)

    async def revoke(self, enrollment_id: UUID, principal: Principal) -> None:
        self._admin(principal)
        await asyncio.to_thread(self.tickets.revoke, enrollment_id)

    async def claim(self, token: str, bootstrap: str) -> EnrollmentGrant:
        lease = await asyncio.to_thread(self.tickets.claim, token, bootstrap)
        if lease.grant:
            return lease.grant
        try:
            key = await self.network.create_cluster_key(lease.expires_at)
        except Exception:
            await asyncio.to_thread(self.tickets.release_lease, lease)
            raise
        # A canceled HTTP request must not drop a key already returned by Headscale.
        save = asyncio.create_task(asyncio.to_thread(self.tickets.save_key, lease, key))
        try:
            return await asyncio.shield(save)
        except asyncio.CancelledError:
            await asyncio.gather(save, return_exceptions=True)
            raise

    def _registration_key(self, token: str, request: EnrollmentRegistration) -> str | ClusterConfig:
        with self.db.transaction() as session:
            row = checked_ticket(session, token, request.bootstrap_secret.get_secret_value())
            if request.config.cluster_id not in {None, row.cluster_id}:
                raise PlatformError(
                    "CLUSTER_ID_CONFLICT", "Enrollment has a different cluster UUID"
                )
            if row.state == "REGISTERED":
                cluster = session.get(Cluster, row.cluster_id)
                if (
                    cluster is None
                    or cluster.state in {"DESTROYED", "POWERED_OFF", "FORCE_POWER_OFF_PENDING"}
                    or not secrets.compare_digest(
                        cluster.agent_token_hash, digest(request.agent_token.get_secret_value())
                    )
                ):
                    raise PlatformError("ENROLLMENT_INVALID", "Registration receipt does not match")
                return ClusterConfig.model_validate(cluster.config)
            if row.state != "CLAIMED" or not row.key_id:
                raise PlatformError(
                    "NODE_NOT_JOINED", "Obtain network credentials before registering"
                )
            return row.key_id

    async def register(self, token: str, request: EnrollmentRegistration) -> ClusterConfig:
        key = await asyncio.to_thread(self._registration_key, token, request)
        if isinstance(key, ClusterConfig):
            return key
        node = await self.network.joined_node(key, request.node_key)
        return await asyncio.to_thread(self._commit_registration, token, request, node)

    def _commit_registration(
        self, token: str, request: EnrollmentRegistration, node: NetworkNode
    ) -> ClusterConfig:
        with self.db.transaction(placement=True) as session:
            row = checked_ticket(session, token, request.bootstrap_secret.get_secret_value())
            if row.state == "REGISTERED":
                cluster = session.get(Cluster, row.cluster_id)
                assert cluster is not None
                if cluster.state in {
                    "DESTROYED",
                    "POWERED_OFF",
                    "FORCE_POWER_OFF_PENDING",
                } or not secrets.compare_digest(
                    cluster.agent_token_hash, digest(request.agent_token.get_secret_value())
                ):
                    raise PlatformError(
                        "ENROLLMENT_INVALID", "Agent credential changed during registration"
                    )
                return ClusterConfig.model_validate(cluster.config)
            if row.state != "CLAIMED":
                raise PlatformError(
                    "ENROLLMENT_INVALID", "Enrollment was retired during registration"
                )
            existing = session.scalar(
                select(Cluster).where(Cluster.headscale_node_id == node.node_id)
            )
            if existing:
                raise PlatformError("NODE_IDENTITY", "Headscale node already belongs to a cluster")
            registered = Registry.register_transaction(
                session,
                request.config,
                request.agent_token.get_secret_value(),
                row.cluster_id,
                node_id=node.node_id,
            )
            cluster = session.get(Cluster, row.cluster_id)
            assert cluster is not None
            cluster.headscale_addresses = list(node.addresses)
            row.state, row.key_ciphertext = "REGISTERED", None
            session.add(Event(cluster_id=row.cluster_id, type="CLUSTER_REGISTERED", payload={}))
            return registered

    def unregister(self, cluster_id: UUID, token: str, snapshot: ClusterSnapshot) -> None:
        with self.db.transaction(placement=True) as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is None or not secrets.compare_digest(
                cluster.agent_token_hash, digest(token)
            ):
                raise PlatformError("UNAUTHORIZED_AGENT", "Agent credentials are invalid")
            retired = session.scalar(
                select(Event.event_id).where(
                    Event.cluster_id == cluster_id, Event.type == "CLUSTER_UNREGISTERED"
                )
            )
            if retired is not None:
                return
            expected_boards = set(
                session.scalars(select(Board.board_id).where(Board.cluster_id == cluster_id))
            )
            if (
                snapshot.state != "DESTROYED"
                or any(not job.state.terminal for job in snapshot.jobs)
                or any(board.active_job for board in snapshot.boards)
                or not expected_boards.issubset({board.board_id for board in snapshot.boards})
                or any(snapshot.queues.values())
            ):
                raise PlatformError(
                    "CLUSTER_NOT_DRAINED", "Drain the local cluster before unregistering"
                )
            finish_unseen_assignments(session, cluster_id, snapshot)
            apply_snapshot(session, cluster, snapshot)
            for board in session.scalars(select(Board).where(Board.cluster_id == cluster_id)):
                board.enabled = False
            cluster.state, cluster.desired_state = "DESTROYED", None
            cluster.health = "OFFLINE"
            cluster.current_session = None
            if cluster.headscale_node_id:
                queue_revocation(session, "NODE", cluster.headscale_node_id, cluster_id=cluster_id)
            session.add(Event(cluster_id=cluster_id, type="CLUSTER_UNREGISTERED", payload={}))
