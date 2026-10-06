"""Commands carry transport scopes; trusted job specifications contain no credentials."""

from uuid import UUID

from pydantic import model_validator

from fl_common.models import JobRecord, JobSpec
from fl_common.models.base import Schema
from fl_common.models.transfer import TransferEndpoint, TransferGrant
from fl_common.ssh import public_key


class StageCommand(Schema):
    spec: JobSpec
    board_id: str
    transfer_id: UUID | None = None


class StageReceipt(Schema):
    record: JobRecord
    transfer_id: UUID
    public_key: str

    @model_validator(mode="after")
    def identity(self) -> "StageReceipt":
        public_key(self.public_key)
        return self


class FetchCommand(Schema):
    job_id: UUID
    endpoint: TransferEndpoint
    grant: TransferGrant

    @model_validator(mode="after")
    def download(self) -> "FetchCommand":
        if (
            self.grant.direction != "download"
            or self.grant.public_download
            or self.grant.job_id != self.job_id
        ):
            raise ValueError("Agent fetch requires a download scope for this job")
        return self
