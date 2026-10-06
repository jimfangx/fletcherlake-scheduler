"""Durable Mac exports and owner-bound public read scopes."""

from alembic import op

revision = "0009_artifacts"
down_revision = "0008_submission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE artifact_exports (
            export_id UUID PRIMARY KEY, job_id UUID NOT NULL REFERENCES jobs(job_id),
            manifest_hash VARCHAR(64) NOT NULL, artifacts JSON NOT NULL, "grant" JSON,
            state VARCHAR(32) NOT NULL, deadline TIMESTAMPTZ NOT NULL, attempts INTEGER NOT NULL,
            next_attempt_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL,
            error VARCHAR(128)
        );
        CREATE INDEX ix_artifact_exports_job_id ON artifact_exports(job_id);
        CREATE INDEX ix_artifact_exports_manifest_hash ON artifact_exports(manifest_hash);
        CREATE TABLE artifact_downloads (
            download_id UUID PRIMARY KEY, job_id UUID NOT NULL REFERENCES jobs(job_id),
            export_id UUID NOT NULL REFERENCES artifact_exports(export_id),
            owner VARCHAR(320) NOT NULL,
            request_id UUID NOT NULL, request_hash VARCHAR(64) NOT NULL,
            public_key VARCHAR(200) UNIQUE NOT NULL, "grant" JSON, closed BOOLEAN NOT NULL,
            CONSTRAINT download_request_identity UNIQUE(owner,request_id)
        );
        CREATE INDEX ix_artifact_downloads_export_id ON artifact_downloads(export_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE artifact_downloads, artifact_exports")
