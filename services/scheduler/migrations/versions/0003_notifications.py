"""Durable event-consumer delivery attempts, separate from job executors."""

from alembic import op

revision = "0003_notifications"
down_revision = "0002_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE notifications (
            notification_id UUID PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES events(event_id),
            channel VARCHAR(32) NOT NULL,
            state VARCHAR(32) NOT NULL,
            attempts INTEGER NOT NULL,
            next_attempt_at TIMESTAMPTZ NOT NULL,
            error TEXT
        );
        CREATE INDEX ix_notifications_event_id ON notifications(event_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE notifications")
