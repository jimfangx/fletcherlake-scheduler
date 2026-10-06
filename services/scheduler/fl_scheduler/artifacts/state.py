"""Short transactions fence export publication, deletion, retries and final verification."""

from datetime import timedelta
from uuid import UUID

from fl_common.models.base import utcnow
from fl_common.models.transfer import TransferGrant
from fl_common.protocol import Message, MessageType
from fl_common.protocol.exports import PublishCommand
from sqlalchemy import select

from ..api.control import add_command
from ..db.models import Assignment
from .models import Download, Export
from .queries import ExportQueries


class ExportState(ExportQueries):
    def grant(self, export_id: UUID, grant: TransferGrant) -> None:
        with self.db.transaction(placement=True) as session:
            row = session.get(Export, export_id)
            assert row is not None
            if row.grant is None:
                row.grant = grant.model_dump(mode="json")

    def publish(self, command: PublishCommand) -> None:
        with self.db.transaction(placement=True) as session:
            work = self.load(session, command.grant.transfer_id)
            row = session.get(Export, command.grant.transfer_id)
            assert row is not None
            if (
                row.state not in {"PREPARING", "UPLOADING"}
                or not work.valid
                or row.deadline <= utcnow()
                or row.next_attempt_at > utcnow()
                or work.publishing
                or (work.publish and work.publish.accepted)
            ):
                return
            assignment = session.get(Assignment, command.job_id)
            assert assignment is not None
            add_command(
                session,
                assignment.cluster_id,
                Message(
                    type=MessageType.ARTIFACT_PUBLISH,
                    payload=command.model_dump(mode="json"),
                ),
                command.job_id,
            )
            row.state, row.attempts = "UPLOADING", row.attempts + 1
            row.next_attempt_at = utcnow() + timedelta(seconds=min(60, 2 ** min(row.attempts, 5)))

    def ready(self, export_id: UUID) -> None:
        with self.db.transaction(placement=True) as session:
            work = self.load(session, export_id)
            row = session.get(Export, export_id)
            assert row is not None
            if row.state in {"PREPARING", "UPLOADING"} and work.valid:
                row.state = "READY"

    def close(self, export_id: UUID, reason: str) -> None:
        with self.db.transaction(placement=True) as session:
            row = session.get(Export, export_id)
            assert row is not None
            row.state, row.error = "CLOSING", reason

    def revoked(self, export_id: UUID) -> None:
        with self.db.transaction(placement=True) as session:
            row = session.get(Export, export_id)
            assert row is not None
            for download in session.scalars(
                select(Download).where(Download.export_id == export_id)
            ):
                download.closed = True
            # Keep revoking until a late, initially absent registration cannot
            # recreate unexpired credentials after a process crash.
            if row.deadline <= utcnow():
                row.state = "CLOSED"

    def defer(self, export_id: UUID) -> None:
        with self.db.transaction(placement=True) as session:
            row = session.get(Export, export_id)
            assert row is not None
            row.next_attempt_at = max(row.next_attempt_at, utcnow() + timedelta(seconds=2))
