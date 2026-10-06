"""Inventory registration and authentication for enrolled agent identities."""

import hashlib
import hmac
from uuid import UUID, uuid4

from fl_common.errors import PlatformError
from fl_common.models import ClusterConfig
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.core import Database
from .db.models import Board, Cluster
from .network.retirement import transfer_retired_board


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Registry:
    def __init__(self, db: Database) -> None:
        self.db = db

    def register(self, config: ClusterConfig, agent_token: str) -> ClusterConfig:
        """Internal enrollment operation; public callers must first consume an enrollment token."""
        with self.db.transaction(placement=True) as session:
            registered = self.register_transaction(session, config, agent_token, uuid4())
        return registered

    @classmethod
    def register_transaction(
        cls,
        session: Session,
        config: ClusterConfig,
        agent_token: str,
        cluster_id: UUID,
        *,
        node_id: str | None = None,
    ) -> ClusterConfig:
        """Enrollment consumes its ticket atomically with inventory registration."""
        registered = config.model_copy(update={"cluster_id": cluster_id})
        cluster = Cluster(
            cluster_id=cluster_id,
            config=registered.model_dump(mode="json"),
            power_control=registered.power_control.model_dump(mode="json")
            if registered.power_control
            else None,
            agent_token_hash=token_hash(agent_token),
            headscale_node_id=node_id,
        )
        session.add(cluster)
        session.flush()
        cls._inventory(session, cluster, registered)
        return registered

    @staticmethod
    def _inventory(session: Session, cluster: Cluster, config: ClusterConfig) -> None:
        present = {board.board_id for board in config.boards if board is not None}
        for existing in session.scalars(
            select(Board).where(Board.cluster_id == cluster.cluster_id)
        ):
            existing.enabled = existing.board_id in present
        for board_config in config.boards:
            if board_config is None:
                continue
            board = session.get(Board, board_config.board_id)
            if board is not None and board.cluster_id != cluster.cluster_id:
                transfer_retired_board(session, board, cluster.cluster_id)
            if board is None:
                board = Board(board_id=board_config.board_id, cluster_id=cluster.cluster_id)
                session.add(board)
            board.enabled = True
            board.config = board_config.model_dump(mode="json")

    def authenticate(self, cluster_id: UUID, agent_token: str) -> None:
        with self.db.transaction() as session:
            cluster = session.get(Cluster, cluster_id)
            if (
                cluster is None
                or cluster.state in {"DESTROYED", "POWERED_OFF", "FORCE_POWER_OFF_PENDING"}
                or not hmac.compare_digest(
                    cluster.agent_token_hash,
                    token_hash(agent_token),
                )
            ):
                raise PlatformError("UNAUTHORIZED_AGENT", "Agent credentials are invalid")

    def update_config(
        self, cluster_id: UUID, config: ClusterConfig, *, session_id: UUID | None = None
    ) -> None:
        if config.cluster_id != cluster_id:
            raise PlatformError(
                "CLUSTER_ID_CONFLICT", "Configuration does not match authenticated cluster"
            )
        with self.db.transaction(placement=True) as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is None:
                raise PlatformError("CLUSTER_NOT_FOUND", "Cluster is not registered")
            if cluster.state in {"DESTROYED", "POWERED_OFF", "FORCE_POWER_OFF_PENDING"}:
                raise PlatformError("UNAUTHORIZED_AGENT", "Retired inventory cannot be changed")
            if session_id is not None and cluster.current_session != session_id:
                raise PlatformError("STALE_SESSION", "Agent inventory session is no longer current")
            self._inventory(session, cluster, config)
            cluster.config = config.model_dump(mode="json")
            cluster.power_control = (
                config.power_control.model_dump(mode="json") if config.power_control else None
            )
