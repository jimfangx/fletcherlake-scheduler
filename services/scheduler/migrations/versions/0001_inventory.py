"""Initial PostgreSQL inventory. SQL is frozen here rather than importing evolving ORM models."""

from alembic import op

revision = "0001_inventory"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE users (
            email VARCHAR(320) PRIMARY KEY,
            subject VARCHAR(255) NOT NULL UNIQUE,
            last_login_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE clusters (
            cluster_id UUID PRIMARY KEY,
            config JSON NOT NULL,
            power_control JSON,
            state VARCHAR(64) NOT NULL,
            health VARCHAR(16) NOT NULL,
            last_heartbeat TIMESTAMPTZ,
            event_sequence INTEGER NOT NULL,
            snapshot JSON,
            agent_token_hash VARCHAR(64) NOT NULL UNIQUE,
            headscale_node_id VARCHAR(64),
            current_session UUID
        );
        CREATE TABLE boards (
            board_id VARCHAR(128) PRIMARY KEY,
            cluster_id UUID NOT NULL REFERENCES clusters(cluster_id),
            config JSON NOT NULL,
            state VARCHAR(64) NOT NULL,
            enabled BOOLEAN NOT NULL,
            active_job UUID
        );
        CREATE INDEX ix_boards_cluster_id ON boards(cluster_id);
        CREATE TABLE agent_sessions (
            session_id UUID PRIMARY KEY,
            cluster_id UUID NOT NULL REFERENCES clusters(cluster_id),
            connected_at TIMESTAMPTZ NOT NULL,
            disconnected_at TIMESTAMPTZ
        );
        CREATE INDEX ix_agent_sessions_cluster_id ON agent_sessions(cluster_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE agent_sessions, boards, clusters, users")
