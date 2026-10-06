"""Atomic download requests reuse pending exports of an identical retained manifest."""

from datetime import timedelta
from uuid import UUID, uuid4

from fl_common.errors import PlatformError
from fl_common.models import JobState
from fl_common.models.base import utcnow
from fl_common.models.download import DownloadRequest
from fl_common.models.scheduler import Principal
from fl_common.protocol import Message, MessageType
from fl_common.protocol.exports import ExportCommand
from sqlalchemy import or_, select

from ..api.control import add_command
from ..api.queries import owned_job
from ..db.core import Database
from ..db.models import Assignment
from .manifest import fingerprint, records
from .models import Download, Export


class Requests:
    def __init__(self, db: Database) -> None:
        self.db = db

    def begin(self, job_id: UUID, body: DownloadRequest, principal: Principal) -> UUID:
        with self.db.transaction(placement=True) as session:
            job = owned_job(session, job_id, principal)
            if not JobState(job.state).terminal:
                raise PlatformError("JOB_ACTIVE", "Results require a terminal job")
            request_hash = fingerprint({"job_id": str(job_id), **body.model_dump(mode="json")})
            previous = session.scalar(
                select(Download).where(
                    Download.owner == principal.email, Download.request_id == body.request_id
                )
            )
            if previous:
                if previous.request_hash != request_hash:
                    raise PlatformError(
                        "DOWNLOAD_ID_CONFLICT", "Download ID has different metadata"
                    )
                return previous.download_id
            if session.scalar(
                select(Download.download_id).where(Download.public_key == body.public_key)
            ):
                raise PlatformError("TRANSFER_IDENTITY", "Use a separate download identity")
            artifacts = records(session, job_id, body.kinds)
            encoded = [record.model_dump(mode="json") for record in artifacts]
            manifest_hash = fingerprint(encoded)
            export = session.scalar(
                select(Export)
                .where(
                    Export.job_id == job_id,
                    Export.manifest_hash == manifest_hash,
                    Export.state.in_(["PREPARING", "UPLOADING", "READY"]),
                    or_(Export.state == "READY", Export.deadline > utcnow()),
                )
                .order_by(Export.created_at.desc())
                .limit(1)
            )
            if export is None:
                assignment = session.get(Assignment, job_id)
                if assignment is None:
                    raise PlatformError("ARTIFACT_NOT_FOUND", "Job has no assigned Mac")
                retains = min(record.expires_at for record in artifacts if record.expires_at)
                export = Export(
                    export_id=uuid4(),
                    job_id=job_id,
                    manifest_hash=manifest_hash,
                    artifacts=encoded,
                    deadline=min(retains, utcnow() + timedelta(minutes=10)),
                )
                session.add(export)
                add_command(
                    session,
                    assignment.cluster_id,
                    Message(
                        type=MessageType.ARTIFACT_PREPARE,
                        payload=ExportCommand(
                            job_id=job_id, transfer_id=export.export_id, artifacts=artifacts
                        ).model_dump(mode="json"),
                    ),
                    job_id,
                )
                session.flush()
            download = Download(
                download_id=uuid4(),
                job_id=job_id,
                export_id=export.export_id,
                owner=principal.email,
                request_id=body.request_id,
                request_hash=request_hash,
                public_key=body.public_key,
            )
            session.add(download)
            return download.download_id
