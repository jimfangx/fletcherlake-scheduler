"""Idempotent public submissions and owner-bound upload identities."""

from alembic import op

revision = "0008_submission"
down_revision = "0007_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE jobs ADD COLUMN submission_id UUID;
        ALTER TABLE jobs ADD COLUMN submission_hash VARCHAR(64);
        ALTER TABLE jobs ADD CONSTRAINT job_submission_identity UNIQUE(owner,submission_id);
        CREATE TABLE job_uploads (
            job_id UUID PRIMARY KEY REFERENCES jobs(job_id),
            upload_id UUID UNIQUE NOT NULL, public_key VARCHAR(200) UNIQUE NOT NULL,
            token_hash VARCHAR(64) NOT NULL, "grant" JSON, closed BOOLEAN NOT NULL,
            next_cleanup_at TIMESTAMPTZ NOT NULL
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE job_uploads")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT job_submission_identity")
    op.execute("ALTER TABLE jobs DROP COLUMN submission_id, DROP COLUMN submission_hash")
