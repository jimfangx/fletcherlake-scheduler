"""Durable verified gateway-to-agent delivery progress."""

from alembic import op

revision = "0007_delivery"
down_revision = "0006_enrollment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE transfer_deliveries (
            job_id UUID PRIMARY KEY REFERENCES jobs(job_id),
            transfer_id UUID UNIQUE NOT NULL, source JSON NOT NULL,
            state VARCHAR(32) NOT NULL, deadline TIMESTAMPTZ NOT NULL,
            attempts INTEGER NOT NULL, next_attempt_at TIMESTAMPTZ NOT NULL,
            error VARCHAR(128)
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE transfer_deliveries")
