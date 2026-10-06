"""Owner-authorized public read scopes contain only metadata and a public identity."""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from fl_common.ssh import public_key

from .artifact import ArtifactKind, ArtifactRecord
from .base import Schema
from .transfer import TransferEndpoint, TransferGrant


class DownloadRequest(Schema):
    request_id: UUID
    public_key: str
    kinds: list[ArtifactKind] = Field(min_length=1, max_length=6)

    @field_validator("public_key")
    @classmethod
    def key(cls, value: str) -> str:
        return public_key(value)

    @field_validator("kinds")
    @classmethod
    def unique(cls, value: list[ArtifactKind]) -> list[ArtifactKind]:
        if len(set(value)) != len(value):
            raise ValueError("Download kinds must be unique")
        return sorted(value)


class DownloadTicket(Schema):
    job_id: UUID
    download_id: UUID
    state: Literal["WAITING", "READY"]
    artifacts: list[ArtifactRecord]
    endpoint: TransferEndpoint | None = None
    grant: TransferGrant | None = None

    @model_validator(mode="after")
    def scope(self) -> "DownloadTicket":
        if self.state == "READY":
            if self.endpoint is None or self.grant is None:
                raise ValueError("Ready downloads require an endpoint and scope")
            if (
                self.grant.job_id != self.job_id
                or self.grant.transfer_id != self.download_id
                or not self.grant.public_download
                or self.grant.files != [record.ref for record in self.artifacts]
            ):
                raise ValueError("Download grant must match this ticket")
        elif self.endpoint is not None or self.grant is not None:
            raise ValueError("Waiting downloads expose no transport credentials")
        return self
