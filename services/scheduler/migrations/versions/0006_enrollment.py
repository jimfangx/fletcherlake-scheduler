"""One-use cluster enrollment and durable Headscale membership cleanup."""

from alembic import op

revision = "0006_enrollment"
down_revision = "0005_job_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE enrollments (
            enrollment_id UUID PRIMARY KEY, cluster_id UUID UNIQUE NOT NULL,
            token_hash VARCHAR(64) UNIQUE NOT NULL, created_by VARCHAR(255) NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL, state VARCHAR(32) NOT NULL,
            bootstrap_hash VARCHAR(64), lease_id UUID, lease_expires_at TIMESTAMPTZ,
            key_id VARCHAR(64), key_ciphertext TEXT, key_expires_at TIMESTAMPTZ
        );
        ALTER TABLE clusters ADD CONSTRAINT clusters_headscale_node_id_key
            UNIQUE(headscale_node_id);
        ALTER TABLE clusters ADD COLUMN headscale_addresses JSON;
        CREATE INDEX ix_enrollments_expires_at ON enrollments(expires_at);
        CREATE TABLE network_revocations (
            revocation_id UUID PRIMARY KEY, cluster_id UUID REFERENCES clusters(cluster_id),
            kind VARCHAR(16) NOT NULL, object_id VARCHAR(64) NOT NULL,
            state VARCHAR(16) NOT NULL, attempts INTEGER NOT NULL,
            next_attempt_at TIMESTAMPTZ NOT NULL, finish_after TIMESTAMPTZ, error VARCHAR(128),
            CONSTRAINT network_revocation_identity UNIQUE(kind, object_id)
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE network_revocations, enrollments")
    op.execute("ALTER TABLE clusters DROP CONSTRAINT clusters_headscale_node_id_key")
    op.execute("ALTER TABLE clusters DROP COLUMN headscale_addresses")
