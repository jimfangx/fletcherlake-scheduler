"""Mac-initiated BBCP pulls, bounded by a durable scope and local staging state."""

import asyncio
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from fl_common.async_calls import background_call
from fl_common.bbcp import BBCP
from fl_common.errors import PlatformError
from fl_common.files import atomic_write, fsync_directory, sha256_file
from fl_common.models import JobRecord, JobState
from fl_common.protocol.delivery import FetchCommand, StageReceipt
from fl_common.ssh import create_identity, identity_public_key

from .collateral import CollateralStore
from .db import AgentDB


class AgentTransfers:
    def __init__(
        self,
        db: AgentDB,
        store: CollateralStore,
        is_ready: Callable[[], bool],
        transport: BBCP | None = None,
        executable: str | None = None,
    ) -> None:
        self.db, self.store, self.is_ready, self.transport = db, store, is_ready, transport
        self.executable = executable
        self.tasks: dict[UUID, asyncio.Task[object]] = {}
        self.locks: dict[UUID, asyncio.Lock] = {}

    def identity(self, job_id: UUID, transfer_id: UUID) -> Path:
        return self.store.directory(job_id) / ".transfer" / str(transfer_id) / "identity"

    def prepare(self, record: JobRecord, transfer_id: UUID) -> StageReceipt:
        if record.state != JobState.STAGING:
            raise PlatformError("JOB_NOT_STAGING", "Transfer requires a locally staged job")
        identity = self.identity(record.spec.job_id, transfer_id)
        identity.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        binding = identity.with_name("scope.json")
        scope = {"spec": record.spec.model_dump(mode="json"), "board_id": record.board_id}
        if binding.exists() and json.loads(binding.read_text()) != scope:
            raise PlatformError("TRANSFER_ID_CONFLICT", "Transfer identity has a different scope")
        if binding.exists() and not identity.exists():
            raise PlatformError("TRANSFER_IDENTITY", "Previously acknowledged identity is absent")
        key = identity_public_key(identity) if identity.exists() else create_identity(identity)
        atomic_write(binding, json.dumps(scope).encode())
        return StageReceipt(record=record, transfer_id=transfer_id, public_key=key)

    def cancel(self, job_id: UUID) -> None:
        task = self.tasks.get(job_id)
        if task:
            task.cancel()

    async def stop(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def check(self, job_id: UUID) -> JobRecord:
        record = self.db.get(job_id)
        if record.state != JobState.STAGING:
            raise PlatformError("JOB_NOT_STAGING", "Job no longer accepts transferred inputs")
        if not self.is_ready():
            raise PlatformError("CLUSTER_NOT_READY", "Cluster no longer accepts staging")
        return record

    async def fetch(self, command: FetchCommand) -> dict[str, object]:
        async with self.locks.setdefault(command.job_id, asyncio.Lock()):
            task = asyncio.current_task()
            assert task is not None
            self.tasks[command.job_id] = task
            try:
                record = self.check(command.job_id)
                expected = {
                    ref.kind: ref for ref in (record.spec.binary, record.spec.bitstream) if ref
                }
                if {ref.kind: ref for ref in command.grant.files} != expected:
                    raise PlatformError(
                        "TRANSFER_SCOPE", "Fetch manifest differs from trusted inputs"
                    )
                identity = self.identity(command.job_id, command.grant.transfer_id)
                key = await background_call(identity_public_key, identity)
                if key != command.grant.public_key:
                    raise PlatformError(
                        "TRANSFER_IDENTITY", "Fetch does not use the staged identity"
                    )
                transport = self.transport or BBCP(self.executable)
                for ref in command.grant.files:
                    self.check(command.job_id)
                    target = self.store.path(command.job_id, ref.kind)
                    if await background_call(valid_file, target, ref.sha256, ref.size_bytes):
                        continue
                    # Retry uses a fresh file; partial content never becomes a trusted input.
                    descriptor, name = tempfile.mkstemp(prefix=".fetch-", dir=target.parent)
                    os.close(descriptor)
                    partial = Path(name)
                    try:
                        await transport.copy(
                            partial, command.endpoint, command.grant, ref.kind, identity
                        )
                        if not await background_call(
                            valid_file, partial, ref.sha256, ref.size_bytes
                        ):
                            raise PlatformError("ARTIFACT_INTEGRITY", "Mac SHA verification failed")
                        self.check(command.job_id)
                        await background_call(publish, partial, target)
                    finally:
                        await background_call(partial.unlink, missing_ok=True)
                await background_call(self.store.verify, record.spec)
                self.check(command.job_id)
                self.db.record_event("JOB_INPUTS_VERIFIED", job_id=command.job_id)
                return {
                    "transfer_id": str(command.grant.transfer_id),
                    "files": [ref.model_dump(mode="json") for ref in command.grant.files],
                }
            except asyncio.CancelledError:
                if self.db.get(command.job_id).state.terminal:
                    raise PlatformError(
                        "JOB_CANCELED", "Job was canceled during transfer"
                    ) from None
                if not self.is_ready():
                    raise PlatformError(
                        "CLUSTER_NOT_READY", "Cluster stopped accepting transfers"
                    ) from None
                raise
            finally:
                self.tasks.pop(command.job_id, None)


def valid_file(path: Path, digest: str, size: int) -> bool:
    return not path.is_symlink() and path.is_file() and sha256_file(path) == (digest, size)


def publish(partial: Path, target: Path) -> None:
    with partial.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(partial, target)
    fsync_directory(target.parent)
