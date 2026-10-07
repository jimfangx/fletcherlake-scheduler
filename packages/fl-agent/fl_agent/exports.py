"""Mac-initiated, replayable export of immutable terminal collateral through Rclone."""

import asyncio
import json
from pathlib import Path
from uuid import UUID

from fl_common.async_calls import background_call
from fl_common.errors import PlatformError
from fl_common.files import atomic_write
from fl_common.models.base import utcnow
from fl_common.protocol.exports import ExportCommand, ExportReceipt, PublishCommand
from fl_common.rclone import Rclone
from fl_common.ssh import create_identity, identity_public_key

from .collateral import CollateralStore
from .db import AgentDB
from .transfers import valid_file


class AgentExports:
    def __init__(self, db: AgentDB, store: CollateralStore, executable: str | None = None) -> None:
        self.db, self.store, self.executable = db, store, executable
        self.transport: Rclone | None = None
        self.tasks: dict[UUID, asyncio.Task[object]] = {}
        self.jobs: dict[UUID, UUID] = {}
        self.locks: dict[UUID, asyncio.Lock] = {}

    def identity(self, job_id: UUID, transfer_id: UUID) -> Path:
        return self.store.directory(job_id) / ".export" / str(transfer_id) / "identity"

    def check(self, command: ExportCommand) -> None:
        if not self.db.get(command.job_id).state.terminal:
            raise PlatformError("JOB_ACTIVE", "Only terminal collateral can be exported")
        if self.db.connection.execute(
            "SELECT 1 FROM collateral_deletion WHERE job_id=?", (str(command.job_id),)
        ).fetchone():
            raise PlatformError("ARTIFACT_EXPIRED", "Collateral deletion was requested")
        records = {
            record.ref.kind: record
            for record in self.store.records()
            if record.job_id == command.job_id
        }
        for expected in command.artifacts:
            current = records.get(expected.ref.kind)
            if current != expected or current.expires_at is None or current.expires_at <= utcnow():
                raise PlatformError("ARTIFACT_EXPIRED", "Export manifest changed or expired")

    async def prepare(self, command: ExportCommand) -> dict[str, object]:
        async with self.locks.setdefault(command.transfer_id, asyncio.Lock()):
            self.check(command)
            for record in command.artifacts:
                if not await background_call(
                    valid_file,
                    self.store.path(command.job_id, record.ref.kind),
                    record.ref.sha256,
                    record.ref.size_bytes,
                ):
                    raise PlatformError("ARTIFACT_INTEGRITY", "Mac collateral changed")
            self.check(command)
            identity = self.identity(command.job_id, command.transfer_id)
            identity.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            binding = identity.with_name("scope.json")
            scope = command.model_dump(mode="json")
            if binding.exists() and json.loads(binding.read_text()) != scope:
                raise PlatformError("TRANSFER_ID_CONFLICT", "Export identity has another manifest")
            if binding.exists() and not identity.exists():
                raise PlatformError("TRANSFER_IDENTITY", "Acknowledged export identity is absent")
            key = identity_public_key(identity) if identity.exists() else create_identity(identity)
            atomic_write(binding, json.dumps(scope).encode())
            return ExportReceipt(**scope, public_key=key).model_dump(mode="json")

    async def publish(self, command: PublishCommand) -> dict[str, object]:
        async with self.locks.setdefault(command.grant.transfer_id, asyncio.Lock()):
            task = asyncio.current_task()
            assert task is not None
            self.tasks[command.grant.transfer_id] = task
            self.jobs[command.grant.transfer_id] = command.job_id
            try:
                identity = self.identity(command.job_id, command.grant.transfer_id)
                prepared = ExportCommand.model_validate_json(
                    identity.with_name("scope.json").read_text()
                )
                if (
                    prepared.job_id != command.job_id
                    or prepared.transfer_id != command.grant.transfer_id
                ):
                    raise PlatformError(
                        "TRANSFER_SCOPE", "Publication differs from export identity"
                    )
                if [record.ref for record in prepared.artifacts] != command.grant.files:
                    raise PlatformError("TRANSFER_SCOPE", "Publication manifest changed")
                if identity_public_key(identity) != command.grant.public_key:
                    raise PlatformError("TRANSFER_IDENTITY", "Publication key changed")
                transport = self.transport or Rclone(self.executable)
                for ref in command.grant.files:
                    self.check(prepared)
                    source = self.store.path(command.job_id, ref.kind)
                    if not await background_call(valid_file, source, ref.sha256, ref.size_bytes):
                        raise PlatformError("ARTIFACT_INTEGRITY", "Mac collateral changed")
                    await transport.copy(
                        source, command.endpoint, command.grant, ref.kind, identity
                    )
                self.check(prepared)
                return {
                    "transfer_id": str(command.grant.transfer_id),
                    "files": [ref.model_dump(mode="json") for ref in command.grant.files],
                }
            except asyncio.CancelledError:
                if self.db.connection.execute(
                    "SELECT 1 FROM collateral_deletion WHERE job_id=?", (str(command.job_id),)
                ).fetchone():
                    raise PlatformError(
                        "ARTIFACT_EXPIRED", "Publication canceled by deletion"
                    ) from None
                raise
            finally:
                self.tasks.pop(command.grant.transfer_id, None)
                self.jobs.pop(command.grant.transfer_id, None)

    async def cancel(self, job_id: UUID) -> None:
        tasks = [task for export_id, task in self.tasks.items() if self.jobs[export_id] == job_id]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
