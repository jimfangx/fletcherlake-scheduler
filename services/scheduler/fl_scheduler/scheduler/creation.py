"""Atomic job creation and immutable owner-scoped submission replay."""

from uuid import UUID, uuid4

from fl_common.errors import PlatformError
from fl_common.models import ArtifactRef, BoardConfig, JobConfig, JobSpec
from fl_common.models.scheduler import Principal
from fl_common.scheduling import matches
from sqlalchemy import select

from ..db.core import Database
from ..db.models import Board, Event, Job
from ..transfers.models import Upload


class JobCreation:
    def __init__(self, db: Database) -> None:
        self.db = db

    def submit(
        self,
        config: JobConfig,
        principal: Principal,
        *,
        binary: ArtifactRef | None = None,
        bitstream: ArtifactRef | None = None,
        request_id: UUID | None = None,
        request_hash: str | None = None,
        public_key: str | None = None,
        token_hash: str | None = None,
    ) -> JobSpec:
        if not principal.permits("job:submit"):
            raise PlatformError("FORBIDDEN", "Identity cannot submit jobs")
        if (config.binary is not None) != (binary is not None) or (
            config.bitstream is not None
        ) != (bitstream is not None):
            raise PlatformError(
                "ARTIFACT_MANIFEST", "Each configured input requires a content reference"
            )
        spec = JobSpec.from_config(config, principal.email, binary=binary, bitstream=bitstream)
        with self.db.transaction(placement=True) as session:
            if request_id is not None:
                existing = session.scalar(
                    select(Job).where(Job.owner == principal.email, Job.submission_id == request_id)
                )
                if existing:
                    if existing.submission_hash != request_hash:
                        raise PlatformError(
                            "SUBMISSION_ID_CONFLICT",
                            "Submission ID was used with different metadata",
                        )
                    return JobSpec.model_validate(existing.spec)
            if config.resource_constraints.board:
                board = session.get(Board, config.resource_constraints.board)
                if (
                    board is None
                    or not board.enabled
                    or not matches(
                        BoardConfig.model_validate(board.config),
                        config.resource_constraints,
                    )
                ):
                    raise PlatformError(
                        "NO_MATCHING_BOARD", "Explicit board is unknown or incompatible"
                    )
            session.add(
                Job(
                    job_id=spec.job_id,
                    owner=spec.owner,
                    spec=spec.model_dump(mode="json"),
                    priority=spec.priority,
                    submitted_at=spec.submitted_at,
                    submission_id=request_id,
                    submission_hash=request_hash,
                )
            )
            session.flush()
            if public_key is not None and token_hash is not None:
                if session.scalar(select(Upload.job_id).where(Upload.public_key == public_key)):
                    raise PlatformError(
                        "TRANSFER_IDENTITY", "Use a separate identity for each submission"
                    )
                session.add(
                    Upload(
                        job_id=spec.job_id,
                        upload_id=uuid4(),
                        public_key=public_key,
                        token_hash=token_hash,
                    )
                )
            session.add(Event(job_id=spec.job_id, type="JOB_CREATED", payload={}))
        return spec
