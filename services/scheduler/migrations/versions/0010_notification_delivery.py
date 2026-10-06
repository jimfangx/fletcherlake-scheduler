"""Event projections, provider pacing, fenced delivery leases and warning identities."""

from alembic import op

revision = "0010_notification_delivery"
down_revision = "0009_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE events ADD COLUMN ingested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
        UPDATE events SET ingested_at=timestamp;
        ALTER TABLE events ALTER COLUMN ingested_at DROP DEFAULT;
        CREATE TABLE notification_routes (
            route_id VARCHAR(64) PRIMARY KEY, channel VARCHAR(32) NOT NULL,
            accept_after TIMESTAMPTZ NOT NULL, next_send_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE notification_projections (
            route_id VARCHAR(64) NOT NULL REFERENCES notification_routes(route_id),
            event_id INTEGER NOT NULL REFERENCES events(event_id),
            PRIMARY KEY (route_id,event_id)
        );
        CREATE INDEX ix_notification_projections_event_id ON notification_projections(event_id);
        CREATE TABLE notification_alert_markers (
            key VARCHAR(256) PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events(event_id)
        );
        CREATE INDEX ix_notification_alert_markers_event_id ON notification_alert_markers(event_id);
        ALTER TABLE notifications
            ADD COLUMN budget_start INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN route_id VARCHAR(64) REFERENCES notification_routes(route_id),
            ADD COLUMN destination_hash VARCHAR(64), ADD COLUMN payload JSON,
            ADD COLUMN lease_id UUID, ADD COLUMN lease_until TIMESTAMPTZ,
            ADD COLUMN sent_at TIMESTAMPTZ, ADD COLUMN last_status INTEGER,
            ADD CONSTRAINT notification_delivery_identity
                UNIQUE(event_id,route_id,destination_hash);
        CREATE INDEX notification_due ON notifications(state,next_attempt_at);
        UPDATE notifications SET state='SKIPPED', error='LEGACY_UNROUTED';
        ALTER TABLE notifications ALTER COLUMN budget_start DROP DEFAULT;
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX notification_due;
        ALTER TABLE notifications DROP CONSTRAINT notification_delivery_identity,
            DROP COLUMN budget_start,
            DROP COLUMN route_id, DROP COLUMN destination_hash, DROP COLUMN payload,
            DROP COLUMN lease_id, DROP COLUMN lease_until,
            DROP COLUMN sent_at, DROP COLUMN last_status;
        DROP TABLE notification_alert_markers, notification_projections, notification_routes;
        ALTER TABLE events DROP COLUMN ingested_at;
    """)
