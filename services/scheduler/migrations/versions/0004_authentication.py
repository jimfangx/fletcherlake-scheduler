"""One-use browser/device login challenges and hashed, revocable human credentials."""

from alembic import op

revision = "0004_authentication"
down_revision = "0003_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE oauth_states (
            state_hash VARCHAR(64) PRIMARY KEY, browser_hash VARCHAR(64) NOT NULL,
            verifier TEXT NOT NULL, nonce VARCHAR(128) NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL, return_path VARCHAR(64) NOT NULL
        );
        CREATE INDEX ix_oauth_states_expires_at ON oauth_states(expires_at);
        CREATE TABLE device_logins (
            device_id UUID PRIMARY KEY, secret_hash VARCHAR(64) UNIQUE NOT NULL,
            user_code VARCHAR(32) UNIQUE NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
            subject VARCHAR(255), email VARCHAR(320), consumed_at TIMESTAMPTZ,
            last_poll_at TIMESTAMPTZ
        );
        CREATE INDEX ix_device_logins_expires_at ON device_logins(expires_at);
        CREATE TABLE human_sessions (
            session_id UUID PRIMARY KEY, subject VARCHAR(255) NOT NULL REFERENCES users(subject),
            access_hash VARCHAR(64) UNIQUE NOT NULL, refresh_hash VARCHAR(64) UNIQUE NOT NULL,
            access_expires_at TIMESTAMPTZ NOT NULL, refresh_expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ
        );
        CREATE INDEX ix_human_sessions_subject ON human_sessions(subject);
        CREATE INDEX ix_human_sessions_refresh_expires_at ON human_sessions(refresh_expires_at);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE human_sessions, device_logins, oauth_states")
