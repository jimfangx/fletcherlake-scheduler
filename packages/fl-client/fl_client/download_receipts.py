"""Private, durable download identity and immutable manifest for retrying a public read."""

from pathlib import Path
from uuid import UUID, uuid4

from fl_common.errors import PlatformError
from fl_common.files import atomic_write
from fl_common.locks import ExclusiveLock
from fl_common.models import ArtifactRecord
from fl_common.models.artifact import ArtifactKind
from fl_common.models.base import Schema
from fl_common.models.download import DownloadRequest
from fl_common.models.scheduler import Principal
from fl_common.network import https_origin
from fl_common.private_files import check_private, read_private
from fl_common.ssh import create_identity, identity_public_key
from pydantic import field_validator


class DownloadReceipt(Schema):
    scheduler: str
    job_id: UUID
    request: DownloadRequest
    principal: Principal
    download_id: UUID | None = None
    artifacts: list[ArtifactRecord] = []

    @field_validator("scheduler")
    @classmethod
    def origin(cls, value: str) -> str:
        return https_origin(value)


class DownloadReceipts:
    def __init__(self, destination: Path) -> None:
        self.destination = destination.absolute()
        self.root = self.destination / ".download"
        self.path, self.identity = self.root / "receipt.json", self.root / "identity"

    def lock(self) -> ExclusiveLock:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        check_private(self.root.lstat(), directory=True)
        return ExclusiveLock(self.root / "receipt.lock")

    def load(self) -> DownloadReceipt:
        receipt = DownloadReceipt.model_validate_json(read_private(self.path))
        if identity_public_key(self.identity) != receipt.request.public_key:
            raise PlatformError("TRANSFER_IDENTITY", "Download identity changed")
        return receipt

    def save(self, receipt: DownloadReceipt) -> None:
        atomic_write(self.path, receipt.model_dump_json().encode())

    def prepare(
        self,
        scheduler: str,
        job_id: UUID,
        kinds: list[ArtifactKind],
        principal: Principal,
    ) -> DownloadReceipt:
        if self.identity.exists():
            raise PlatformError(
                "TRANSFER_IDENTITY", "Download receipt is absent; choose another directory"
            )
        key = create_identity(self.identity)
        receipt = DownloadReceipt(
            scheduler=scheduler,
            job_id=job_id,
            principal=principal,
            request=DownloadRequest(request_id=uuid4(), public_key=key, kinds=kinds),
        )
        self.save(receipt)
        return receipt
