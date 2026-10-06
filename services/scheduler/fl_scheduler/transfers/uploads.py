"""Owner-bound upload grants are persisted before external gateway registration."""

from datetime import timedelta
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import JobSpec, JobState
from fl_common.models.base import utcnow
from fl_common.models.scheduler import Principal
from fl_common.models.submission import UploadTicket
from fl_common.models.transfer import TransferEndpoint, TransferGrant

from ..api.queries import owned_job
from ..db.core import Database
from ..db.models import Assignment
from .models import Delivery, Upload


class Uploads:
    def __init__(self, db: Database, endpoint: TransferEndpoint, origin: str) -> None:
        self.db, self.endpoint, self.origin = db, endpoint, origin

    def ticket(self, job_id: UUID, principal: Principal) -> UploadTicket:
        with self.db.transaction(placement=True) as session:
            job = owned_job(session, job_id, principal)
            if job.cancel_requested:
                raise PlatformError(
                    "JOB_CANCELED", "Canceled jobs cannot receive upload credentials"
                )
            result = UploadTicket(job_id=job_id, state="WAITING", job_state=JobState(job.state))
            if JobState(job.state).terminal:
                result.state = "COMPLETE"
                return result
            if session.get(Delivery, job_id) is not None:
                result.state = "DELIVERING"
                return result
            assignment = session.get(Assignment, job_id)
            if assignment and assignment.state in {"DELIVERING", "QUEUED"}:
                result.state = "DELIVERING"
                return result
            if (
                assignment is None
                or assignment.state not in {"RESERVED", "STAGING"}
                or assignment.expires_at <= utcnow()
            ):
                return result
            spec = JobSpec.model_validate(job.spec)
            files = [ref for ref in (spec.binary, spec.bitstream) if ref]
            if not files:
                return result
            upload = session.get(Upload, job_id)
            if upload is None:
                raise PlatformError(
                    "UPLOAD_IDENTITY_REQUIRED", "Use the public submission workflow for collateral"
                )
            if upload.closed:
                raise PlatformError("UPLOAD_EXPIRED", "Upload scope was permanently closed")
            old = TransferGrant.model_validate(upload.grant) if upload.grant else None
            retains = old.retains_until if old else utcnow() + timedelta(days=1)
            expires = min(assignment.expires_at, utcnow() + timedelta(minutes=10), retains)
            if expires <= utcnow():
                raise PlatformError(
                    "UPLOAD_EXPIRED", "Upload retention expired; submit a new request"
                )
            grant = TransferGrant(
                transfer_id=upload.upload_id,
                job_id=job_id,
                direction="upload",
                public_key=upload.public_key,
                token_hash=upload.token_hash,
                files=files,
                expires_at=expires,
                retains_until=retains,
            )
            upload.grant = grant.model_dump(mode="json")
            assignment.state = "STAGING"
            return UploadTicket(
                job_id=job_id,
                state="UPLOAD",
                job_state=JobState(job.state),
                endpoint=self.endpoint,
                verify_origin=self.origin,
                grant=grant,
            )

    def recheck(self, job_id: UUID, principal: Principal) -> None:
        with self.db.transaction(placement=True) as session:
            job = owned_job(session, job_id, principal)
            if job.cancel_requested or JobState(job.state).terminal:
                raise PlatformError("JOB_CANCELED", "Job closed during upload registration")
