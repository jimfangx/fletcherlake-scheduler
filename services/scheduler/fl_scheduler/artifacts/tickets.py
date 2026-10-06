"""Public scopes are registered only after rechecking retained, owner-authorized artifacts."""

import hashlib
from datetime import timedelta
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import ArtifactRecord
from fl_common.models.base import utcnow
from fl_common.models.download import DownloadTicket
from fl_common.models.scheduler import Principal
from fl_common.models.transfer import TransferEndpoint, TransferGrant
from sqlalchemy.orm import Session

from ..api.queries import owned_job
from ..db.core import Database
from .manifest import records
from .models import Download, Export


class Tickets:
    def __init__(self, db: Database, endpoint: TransferEndpoint) -> None:
        self.db, self.endpoint = db, endpoint

    def ticket(self, job_id: UUID, download_id: UUID, principal: Principal) -> DownloadTicket:
        with self.db.transaction(placement=True) as session:
            download, export, artifacts = self.current(session, job_id, download_id, principal)
            if export.state != "READY":
                return DownloadTicket(
                    job_id=job_id, download_id=download_id, state="WAITING", artifacts=artifacts
                )
            retains = min(record.expires_at for record in artifacts if record.expires_at)
            grant = TransferGrant(
                transfer_id=download_id,
                job_id=job_id,
                direction="download",
                public_download=True,
                public_key=download.public_key,
                token_hash=hashlib.sha256(
                    b"public reads require the scoped SSH identity"
                ).hexdigest(),
                files=[record.ref for record in artifacts],
                expires_at=min(retains, utcnow() + timedelta(minutes=10)),
                retains_until=retains,
                source_id=export.export_id,
            )
            download.grant = grant.model_dump(mode="json")
            return DownloadTicket(
                job_id=job_id,
                download_id=download_id,
                state="READY",
                artifacts=artifacts,
                endpoint=self.endpoint,
                grant=grant,
            )

    def current(
        self,
        session: Session,
        job_id: UUID,
        download_id: UUID,
        principal: Principal,
    ) -> tuple[Download, Export, list[ArtifactRecord]]:
        owned_job(session, job_id, principal)
        download = session.get(Download, download_id)
        if download is None or download.job_id != job_id or download.owner != principal.email:
            raise PlatformError("FORBIDDEN", "Download identity belongs to another request")
        export = session.get(Export, download.export_id)
        assert export is not None
        kinds = [value["ref"]["kind"] for value in export.artifacts]
        artifacts = records(session, job_id, kinds)
        if [record.model_dump(mode="json") for record in artifacts] != export.artifacts:
            raise PlatformError("ARTIFACT_EXPIRED", "Export manifest changed")
        if download.closed or export.state in {"CLOSING", "CLOSED"}:
            raise PlatformError("EXPORT_FAILED", export.error or "Export was closed")
        return download, export, artifacts

    def recheck(self, job_id: UUID, download_id: UUID, principal: Principal) -> None:
        with self.db.transaction(placement=True) as session:
            _, export, _ = self.current(session, job_id, download_id, principal)
            if export.state != "READY":
                raise PlatformError("EXPORT_FAILED", "Export closed during read registration")
