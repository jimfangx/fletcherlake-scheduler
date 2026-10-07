"""Download retained results without root, private-network access, or scheduler payload IO."""

import asyncio
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.files import fsync_directory, sha256_file
from fl_common.models import ArtifactRecord
from fl_common.models.artifact import ARTIFACT_FILENAMES
from fl_common.models.base import utcnow
from fl_common.models.download import DownloadTicket
from fl_common.models.scheduler import Principal
from fl_common.rclone import Rclone
from pydantic import TypeAdapter

from .api import RemoteClient
from .download_receipts import DownloadReceipts


def verified(path: Path, artifact: ArtifactRecord) -> bool:
    return (
        not path.is_symlink()
        and path.is_file()
        and sha256_file(path)
        == (
            artifact.ref.sha256,
            artifact.ref.size_bytes,
        )
    )


class Results:
    def __init__(
        self,
        client: RemoteClient,
        *,
        rclone: Rclone | None = None,
        display: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client, self.rclone, self.display, self.sleep = client, rclone, display, sleep

    def run(self, job_id: UUID, destination: Path, *, inputs: bool = False) -> list[Path]:
        store = DownloadReceipts(destination)
        with store.lock():
            origin = self.client.origin()
            principal = Principal.model_validate(self.client.request("GET", "/api/auth/me"))
            if store.path.exists():
                receipt = store.load()
                if (
                    receipt.scheduler != origin
                    or receipt.job_id != job_id
                    or (receipt.principal.email, receipt.principal.subject)
                    != (principal.email, principal.subject)
                ):
                    raise PlatformError("DOWNLOAD_SCOPE", "Resume with the original job and login")
            else:
                artifacts = TypeAdapter(list[ArtifactRecord]).validate_python(
                    self.client.artifacts(job_id)
                )
                kinds = [
                    record.ref.kind
                    for record in artifacts
                    if (inputs or record.ref.kind not in {"binary", "bitstream"})
                    and not record.deleted_at
                    and record.expires_at
                    and record.expires_at > utcnow()
                ]
                if not kinds:
                    raise PlatformError("ARTIFACT_NOT_FOUND", "Job has no retained outputs yet")
                receipt = store.prepare(origin, job_id, kinds, principal)
            self.display("Download receipt: " + str(store.path))
            waiting = False
            while True:
                ticket = DownloadTicket.model_validate(
                    self.client.request(
                        "POST",
                        f"/api/jobs/{job_id}/downloads",
                        json=receipt.request.model_dump(mode="json"),
                    )
                )
                if (
                    ticket.job_id != job_id
                    or (
                        receipt.download_id is not None
                        and receipt.download_id != ticket.download_id
                    )
                    or (receipt.artifacts and receipt.artifacts != ticket.artifacts)
                ):
                    raise PlatformError("DOWNLOAD_SCOPE", "Download ticket changed")
                receipt.download_id, receipt.artifacts = ticket.download_id, ticket.artifacts
                store.save(receipt)
                if ticket.state == "READY":
                    break
                if not waiting:
                    self.display("Waiting for the Mac to export retained artifacts")
                    waiting = True
                self.sleep(2)
            assert ticket.grant is not None and ticket.endpoint is not None
            if ticket.grant.public_key != receipt.request.public_key:
                raise PlatformError("TRANSFER_IDENTITY", "Read grant uses another identity")
            paths = []
            for artifact in ticket.artifacts:
                target = store.destination / ARTIFACT_FILENAMES[artifact.ref.kind]
                paths.append(target)
                if verified(target, artifact):
                    continue
                if target.exists() or target.is_symlink():
                    raise PlatformError(
                        "OUTPUT_EXISTS", "Use another output directory for different files"
                    )
                transport = self.rclone or Rclone()
                descriptor, name = tempfile.mkstemp(prefix=".download-", dir=store.destination)
                os.close(descriptor)
                partial = Path(name)
                try:
                    self.display("Downloading " + artifact.ref.kind)
                    asyncio.run(
                        transport.copy(
                            partial,
                            ticket.endpoint,
                            ticket.grant,
                            artifact.ref.kind,
                            store.identity,
                        )
                    )
                    if not verified(partial, artifact):
                        raise PlatformError(
                            "ARTIFACT_INTEGRITY", "Downloaded bytes failed SHA verification"
                        )
                    with partial.open("rb") as stream:
                        os.fsync(stream.fileno())
                    # An existing file can never be clobbered, even by a concurrent writer.
                    os.link(partial, target, follow_symlinks=False)
                    fsync_directory(store.destination)
                finally:
                    partial.unlink(missing_ok=True)
            return paths
