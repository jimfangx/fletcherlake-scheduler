"""Validate ownership and current retention before looking at any log receipt."""

from datetime import timedelta
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import JobRecord, JobState
from fl_common.models.base import utcnow
from fl_common.models.logs import LogHead, LogStream
from fl_common.protocol import MessageType
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Assignment, Cluster, Command, Job


def current(session: Session, job: Job, stream: LogStream) -> tuple[LogHead | None, UUID | None]:
    deleted = session.scalar(
        select(Command.message_id)
        .where(
            Command.job_id == job.job_id,
            Command.envelope["type"].as_string() == MessageType.ARTIFACT_DELETE,
        )
        .limit(1)
    )
    if deleted:
        raise PlatformError("ARTIFACT_EXPIRED", "Log deletion was requested")
    if job.record:
        record = JobRecord.model_validate(job.record)
        if (
            record.finished_at
            and record.finished_at + timedelta(days=record.spec.collateral_ttl_days) <= utcnow()
        ):
            raise PlatformError("ARTIFACT_EXPIRED", "Log retention expired")
    assignment = session.get(Assignment, job.job_id)
    if assignment is None:
        if JobState(job.state).terminal:
            return LogHead(
                job_id=job.job_id, stream=stream, size_bytes=0, terminal=True, retained=True
            ), None
        return None, None
    cluster = session.get(Cluster, assignment.cluster_id)
    if cluster and cluster.snapshot:
        for data in cluster.snapshot.get("logs", []):
            head = LogHead.model_validate(data)
            if (head.job_id, head.stream) == (job.job_id, stream):
                if not head.retained or (head.expires_at and head.expires_at <= utcnow()):
                    raise PlatformError("ARTIFACT_EXPIRED", "Log retention expired")
                return head, assignment.cluster_id
    return None, assignment.cluster_id
