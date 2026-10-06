"""Current export manifests and durable ACKs, independent of gateway IO."""

from dataclasses import dataclass
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import ArtifactRecord
from fl_common.models.base import utcnow
from fl_common.protocol import Ack, MessageType
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.core import Database
from ..db.models import Assignment, Cluster, Command
from ..transfers.queries import latest
from .manifest import records
from .models import Download, Export


@dataclass(frozen=True)
class Work:
    export: Export
    artifacts: list[ArtifactRecord]
    valid: bool
    networks: tuple[str, ...]
    prepare: Ack | None
    publish: Ack | None
    publishing: bool


class ExportQueries:
    def __init__(self, db: Database) -> None:
        self.db = db

    def pending(self) -> list[UUID]:
        with self.db.transaction() as session:
            return list(
                session.scalars(
                    select(Export.export_id)
                    .where(
                        Export.state != "CLOSED",
                        Export.next_attempt_at <= utcnow(),
                    )
                    .order_by(Export.next_attempt_at, Export.export_id)
                    .limit(100)
                )
            )

    def work(self, export_id: UUID) -> Work:
        with self.db.transaction(placement=True) as session:
            return self.load(session, export_id)

    def load(self, session: Session, export_id: UUID) -> Work:
        row = session.get(Export, export_id)
        assert row is not None
        artifacts = [ArtifactRecord.model_validate(value) for value in row.artifacts]
        try:
            valid = (
                records(session, row.job_id, [record.ref.kind for record in artifacts]) == artifacts
            )
        except PlatformError:
            valid = False
        assignment = session.get(Assignment, row.job_id)
        cluster = session.get(Cluster, assignment.cluster_id) if assignment else None
        networks = (
            tuple(
                f"{address}/{'128' if ':' in address else '32'}"
                for address in (cluster.headscale_addresses or [])
            )
            if cluster
            else ()
        )
        commands = [
            command
            for command in session.scalars(
                select(Command)
                .where(
                    Command.job_id == row.job_id,
                )
                .order_by(Command.created_at, Command.message_id)
            )
            if (
                command.envelope["payload"].get("transfer_id")
                or command.envelope["payload"].get("grant", {}).get("transfer_id")
            )
            == str(export_id)
        ]
        prepare = latest(commands, MessageType.ARTIFACT_PREPARE)
        publish = latest(commands, MessageType.ARTIFACT_PUBLISH)
        return Work(
            row,
            artifacts,
            valid,
            networks,
            Ack.model_validate(prepare.response) if prepare and prepare.response else None,
            Ack.model_validate(publish.response) if publish and publish.response else None,
            publish is not None and publish.acknowledged_at is None,
        )

    def downloads(self, export_id: UUID) -> list[UUID]:
        with self.db.transaction() as session:
            return list(
                session.scalars(
                    select(Download.download_id).where(
                        Download.export_id == export_id,
                        Download.closed.is_(False),
                    )
                )
            )
