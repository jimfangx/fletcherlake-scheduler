"""Retained artifact manifests are always checked against current agent metadata."""

import hashlib
import json
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import ArtifactRecord
from fl_common.models.artifact import ArtifactKind
from fl_common.models.base import utcnow
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Artifact, Command


def records(session: Session, job_id: UUID, kinds: list[ArtifactKind]) -> list[ArtifactRecord]:
    deletion = session.scalar(
        select(Command.message_id)
        .where(Command.job_id == job_id, Command.envelope["type"].as_string() == "ARTIFACT_DELETE")
        .limit(1)
    )
    if deletion:
        raise PlatformError("ARTIFACT_EXPIRED", "Collateral deletion was requested")
    result = []
    for kind in sorted(kinds):
        row = session.get(Artifact, (job_id, kind))
        if row is None:
            raise PlatformError("ARTIFACT_NOT_FOUND", "Requested artifact is absent")
        record = ArtifactRecord.model_validate(row.metadata_json)
        if record.deleted_at or record.expires_at is None or record.expires_at <= utcnow():
            raise PlatformError("ARTIFACT_EXPIRED", "Requested artifact is unavailable or expired")
        result.append(record)
    return result


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
