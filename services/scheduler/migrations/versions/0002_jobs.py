"""Durable scheduling, assignments, replicated artifacts/events, and command outbox."""

from alembic import op

revision = "0002_jobs"
down_revision = "0001_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE jobs (
            job_id UUID PRIMARY KEY,
            owner VARCHAR(320) NOT NULL,
            spec JSON NOT NULL,
            state VARCHAR(64) NOT NULL,
            priority INTEGER NOT NULL,
            submitted_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            record JSON,
            error TEXT
        );
        CREATE INDEX ix_jobs_owner ON jobs(owner);
        CREATE INDEX ix_jobs_state ON jobs(state);
        CREATE INDEX jobs_priority_fifo ON jobs(priority, submitted_at, job_id);
        CREATE TABLE job_assignments (
            job_id UUID PRIMARY KEY REFERENCES jobs(job_id),
            cluster_id UUID NOT NULL REFERENCES clusters(cluster_id),
            board_id VARCHAR(128) NOT NULL REFERENCES boards(board_id),
            state VARCHAR(32) NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX ix_job_assignments_cluster_id ON job_assignments(cluster_id);
        CREATE INDEX ix_job_assignments_board_id ON job_assignments(board_id);
        CREATE INDEX ix_job_assignments_expires_at ON job_assignments(expires_at);
        CREATE UNIQUE INDEX one_transfer_per_board ON job_assignments(board_id)
            WHERE state IN ('RESERVED', 'STAGING');
        CREATE TABLE artifacts (
            job_id UUID NOT NULL REFERENCES jobs(job_id),
            kind VARCHAR(32) NOT NULL,
            metadata_json JSON NOT NULL,
            PRIMARY KEY(job_id, kind)
        );
        CREATE TABLE events (
            event_id SERIAL PRIMARY KEY,
            cluster_id UUID REFERENCES clusters(cluster_id),
            agent_sequence INTEGER,
            job_id UUID REFERENCES jobs(job_id),
            type VARCHAR(64) NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL,
            payload JSON NOT NULL
        );
        CREATE INDEX ix_events_job_id ON events(job_id);
        CREATE UNIQUE INDEX agent_event_identity ON events(cluster_id, agent_sequence);
        CREATE TABLE agent_commands (
            message_id UUID PRIMARY KEY,
            cluster_id UUID NOT NULL REFERENCES clusters(cluster_id),
            job_id UUID REFERENCES jobs(job_id),
            envelope JSON NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            acknowledged_at TIMESTAMPTZ,
            response JSON
        );
        CREATE INDEX ix_agent_commands_cluster_id ON agent_commands(cluster_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE agent_commands, events, artifacts, job_assignments, jobs")
