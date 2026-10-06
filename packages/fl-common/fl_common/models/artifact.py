"""Content references do not contain user-selected storage paths."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from .base import Schema

ArtifactKind = Literal["binary", "bitstream", "stdout", "stderr", "results", "job"]

ARTIFACT_FILENAMES: dict[ArtifactKind, str] = {
    "binary": "binary",
    "bitstream": "bitstream",
    "stdout": "stdout.log",
    "stderr": "stderr.log",
    "results": "results.json",
    "job": "job.json",
}


class ArtifactRef(Schema):
    kind: ArtifactKind
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    original_name: str | None = None


class ArtifactRecord(Schema):
    job_id: UUID
    ref: ArtifactRef
    expires_at: AwareDatetime | None = None
    deleted_at: datetime | None = None
