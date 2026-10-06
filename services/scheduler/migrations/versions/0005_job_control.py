"""Preserve cancellation intent independently of replicated hardware state."""

from alembic import op

revision = "0005_job_control"
down_revision = "0004_authentication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE jobs ALTER COLUMN cancel_requested DROP DEFAULT")
    op.execute("ALTER TABLE clusters ADD COLUMN desired_state VARCHAR(64)")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN cancel_requested")
    op.execute("ALTER TABLE clusters DROP COLUMN desired_state")
