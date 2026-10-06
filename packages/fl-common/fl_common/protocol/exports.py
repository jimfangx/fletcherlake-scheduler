"""Terminal collateral export binds a private Mac identity to an exact retained manifest."""

from uuid import UUID

from pydantic import Field, field_validator, model_validator

from fl_common.models import ArtifactRecord
from fl_common.models.base import Schema
from fl_common.models.transfer import TransferEndpoint, TransferGrant
from fl_common.ssh import public_key


class ExportCommand(Schema):
    job_id: UUID
    transfer_id: UUID
    artifacts: list[ArtifactRecord] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def manifest(self) -> "ExportCommand":
        if len({record.ref.kind for record in self.artifacts}) != len(self.artifacts):
            raise ValueError("Export kinds must be unique")
        if any(record.job_id != self.job_id for record in self.artifacts):
            raise ValueError("Export records must belong to this job")
        if any(record.expires_at is None or record.deleted_at for record in self.artifacts):
            raise ValueError("Export records require finalized retention")
        return self


class ExportReceipt(ExportCommand):
    public_key: str

    @field_validator("public_key")
    @classmethod
    def key(cls, value: str) -> str:
        return public_key(value)


class PublishCommand(Schema):
    job_id: UUID
    endpoint: TransferEndpoint
    grant: TransferGrant

    @model_validator(mode="after")
    def upload(self) -> "PublishCommand":
        if (
            self.grant.direction != "upload"
            or self.grant.job_id != self.job_id
            or not self.grant.source_networks
        ):
            raise ValueError("Mac publication requires a private upload scope for this job")
        return self
