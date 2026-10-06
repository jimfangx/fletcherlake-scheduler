"""Public submission metadata and reserved upload scopes contain no private identity."""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from fl_common.network import https_origin
from fl_common.ssh import public_key

from .artifact import ArtifactRef
from .base import Schema
from .events import JobState
from .job import JobConfig, JobSpec
from .transfer import TransferEndpoint, TransferGrant


class SubmissionRequest(Schema):
    request_id: UUID
    config: JobConfig
    binary: ArtifactRef | None = None
    bitstream: ArtifactRef | None = None
    public_key: str | None = None
    token_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("public_key")
    @classmethod
    def key(cls, value: str | None) -> str | None:
        return public_key(value) if value is not None else None

    @model_validator(mode="after")
    def credentials(self) -> "SubmissionRequest":
        for kind in ("binary", "bitstream"):
            ref = getattr(self, kind)
            if (getattr(self.config, kind) is not None) != (ref is not None):
                raise ValueError("Each configured input requires a content reference")
            if ref is not None and ref.kind != kind:
                raise ValueError("Input reference kind must match its field")
        inputs = self.config.binary is not None or self.config.bitstream is not None
        if inputs != (self.public_key is not None and self.token_hash is not None):
            raise ValueError(
                "Collateral submissions require a public identity and staging-token hash"
            )
        if not inputs and (self.public_key is not None or self.token_hash is not None):
            raise ValueError("A submission without collateral needs no transfer credentials")
        return self


class SubmissionResponse(Schema):
    spec: JobSpec
    state: JobState


class UploadTicket(Schema):
    job_id: UUID
    state: Literal["WAITING", "UPLOAD", "DELIVERING", "COMPLETE"]
    job_state: JobState
    endpoint: TransferEndpoint | None = None
    verify_origin: str | None = None
    grant: TransferGrant | None = None

    @field_validator("verify_origin")
    @classmethod
    def origin(cls, value: str | None) -> str | None:
        return https_origin(value) if value is not None else None

    @model_validator(mode="after")
    def scope(self) -> "UploadTicket":
        if self.state == "UPLOAD":
            if self.endpoint is None or self.verify_origin is None or self.grant is None:
                raise ValueError("An upload ticket requires its endpoint and scope")
            if self.grant.job_id != self.job_id or self.grant.direction != "upload":
                raise ValueError("Upload grant must belong to this job")
        elif self.endpoint is not None or self.verify_origin is not None or self.grant is not None:
            raise ValueError("Only an upload ticket exposes a transport scope")
        return self
