"""Durable gateway scopes. Blob IO never runs inside a SQLite transaction."""

import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.files import fsync_directory, sha256_file
from fl_common.locks import ExclusiveLock
from fl_common.models import ArtifactRef
from fl_common.models.artifact import ArtifactKind
from fl_common.models.base import utcnow
from fl_common.models.transfer import TransferGrant

from .authorized_keys import write_keys


class GatewayStore:
    def __init__(
        self, root: Path, bbcp: Path, *, data_port_first: int = 5000, data_port_last: int = 5099
    ) -> None:
        if not 1024 <= data_port_first <= data_port_last - 7 <= 65528:
            raise ValueError("BBCP requires at least eight unprivileged data ports")
        self.data_port_first, self.data_port_last = data_port_first, data_port_last
        self.root, self.bbcp = root.resolve(), bbcp.resolve()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.root.chmod(0o700)
        with self.connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise PlatformError("DATABASE_VERSION", "Unsupported gateway schema version")
            if version == 0:
                connection.executescript(Path(__file__).with_name("schema.sql").read_text())
                connection.execute("PRAGMA user_version=1")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.root / "gateway.db", timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def lock(self, transfer_id: UUID) -> ExclusiveLock:
        return ExclusiveLock(self.root / "locks" / f"{transfer_id}.lock")

    def register(self, grant: TransferGrant) -> None:
        if not 0 < (grant.expires_at - utcnow()).total_seconds() <= 600:
            raise PlatformError(
                "TRANSFER_EXPIRED", "Transfer credentials require a ten-minute bound"
            )
        if not 0 < (grant.retains_until - utcnow()).total_seconds() <= 365 * 86400:
            raise PlatformError(
                "TRANSFER_RETENTION", "Retention requires a bound of at most 365 days"
            )
        with ExclusiveLock(self.root / "registry.lock"):
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM transfers WHERE transfer_id=?", (str(grant.transfer_id),)
                ).fetchone()
                if row:
                    previous = TransferGrant.model_validate_json(row["grant_json"])
                    if previous.model_dump(exclude={"expires_at"}) != grant.model_dump(
                        exclude={"expires_at"}
                    ):
                        raise PlatformError("TRANSFER_ID_CONFLICT", "Transfer scope is immutable")
                    if row["state"] == "REVOKED":
                        raise PlatformError("TRANSFER_REVOKED", "Transfer was revoked")
                    grant.expires_at = max(grant.expires_at, previous.expires_at)
                    connection.execute(
                        "UPDATE transfers SET grant_json=? WHERE transfer_id=?",
                        (grant.model_dump_json(), str(grant.transfer_id)),
                    )
                else:
                    existing = connection.execute(
                        "SELECT 1 FROM transfers WHERE public_key=?", (grant.public_key,)
                    ).fetchone()
                    if existing:
                        raise PlatformError(
                            "TRANSFER_IDENTITY", "Use a separate key for each scope"
                        )
                    if grant.direction == "download":
                        self.source(grant)
                    connection.execute(
                        "INSERT INTO transfers(transfer_id,public_key,grant_json) VALUES (?,?,?)",
                        (str(grant.transfer_id), grant.public_key, grant.model_dump_json()),
                    )
            self.repair_authorized_keys()

    def lookup(self, transfer_id: UUID, *, active: bool = True) -> tuple[TransferGrant, str]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT grant_json,state FROM transfers WHERE transfer_id=?", (str(transfer_id),)
            ).fetchone()
        if row is None:
            raise PlatformError("TRANSFER_NOT_FOUND", "Unknown transfer scope")
        grant = TransferGrant.model_validate_json(row["grant_json"])
        if active and (row["state"] == "REVOKED" or grant.expires_at <= utcnow()):
            raise PlatformError("TRANSFER_EXPIRED", "Transfer scope is unavailable or expired")
        return grant, row["state"]

    def source(self, grant: TransferGrant) -> TransferGrant:
        assert grant.source_id is not None
        source, state = self.lookup(grant.source_id, active=False)
        expected = {ref.kind: ref for ref in source.files}
        if source.direction != "upload" or state != "VERIFIED" or source.job_id != grant.job_id:
            raise PlatformError("TRANSFER_SOURCE", "Source is not a verified upload for this job")
        if any(expected.get(ref.kind) != ref for ref in grant.files):
            raise PlatformError("TRANSFER_SOURCE", "Download manifest differs from verified upload")
        if source.retains_until <= utcnow() or grant.retains_until > source.retains_until:
            raise PlatformError("TRANSFER_SOURCE", "Download exceeds source retention")
        return source

    def path(self, grant: TransferGrant, kind: ArtifactKind) -> Path:
        if kind not in {ref.kind for ref in grant.files}:
            raise PlatformError("TRANSFER_SCOPE", "Artifact is outside this transfer scope")
        return self.root / "jobs" / str(grant.job_id) / str(grant.transfer_id) / kind

    def received(self, grant: TransferGrant, ref: ArtifactRef, partial: Path) -> None:
        """Publish bytes only while an immediate transaction fences revocation."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT grant_json,state FROM transfers WHERE transfer_id=?",
                (str(grant.transfer_id),),
            ).fetchone()
            if row is None or row["state"] != "OPEN":
                raise PlatformError("TRANSFER_REVOKED", "Transfer no longer accepts uploads")
            current = TransferGrant.model_validate_json(row["grant_json"])
            if current.expires_at <= utcnow() or current.files != grant.files:
                raise PlatformError("TRANSFER_EXPIRED", "Transfer scope changed or expired")
            target = self.path(current, ref.kind)
            os.replace(partial, target)
            fsync_directory(target.parent)
            connection.execute(
                "INSERT INTO received_files VALUES (?,?,?,?) "
                "ON CONFLICT(transfer_id,kind) DO UPDATE SET "
                "sha256=excluded.sha256,size_bytes=excluded.size_bytes",
                (str(grant.transfer_id), ref.kind, ref.sha256, ref.size_bytes),
            )

    def verify(self, transfer_id: UUID, token_hash: str) -> list[ArtifactRef]:
        with self.lock(transfer_id):
            grant, state = self.lookup(transfer_id)
            if grant.direction != "upload" or not secrets.compare_digest(
                grant.token_hash, token_hash
            ):
                raise PlatformError("TRANSFER_UNAUTHORIZED", "Scoped upload credential required")
            for ref in grant.files:
                path = self.path(grant, ref.kind)
                if path.is_symlink() or not path.is_file():
                    raise PlatformError("TRANSFER_INCOMPLETE", "An expected artifact is absent")
                if sha256_file(path) != (ref.sha256, ref.size_bytes):
                    raise PlatformError("ARTIFACT_INTEGRITY", "Gateway SHA verification failed")
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state,grant_json FROM transfers WHERE transfer_id=?",
                    (str(transfer_id),),
                ).fetchone()
                current = TransferGrant.model_validate_json(row["grant_json"])
                if row["state"] == "REVOKED" or current.expires_at <= utcnow():
                    raise PlatformError(
                        "TRANSFER_REVOKED", "Upload expired or was revoked during verification"
                    )
                connection.execute(
                    "UPDATE transfers SET state='VERIFIED' WHERE transfer_id=?", (str(transfer_id),)
                )
            return grant.files

    def revoke(self, transfer_id: UUID) -> None:
        # Helpers check state again while committing; revocation need not wait for their IO.
        with ExclusiveLock(self.root / "registry.lock"):
            with self.connection() as connection:
                connection.execute(
                    "UPDATE transfers SET state='REVOKED' WHERE transfer_id=?", (str(transfer_id),)
                )
            self.repair_authorized_keys()

    def repair_authorized_keys(self) -> None:
        """Caller holds the registry lock; retry/startup repairs a crash after DB commit."""
        with self.connection() as connection:
            rows = connection.execute("SELECT grant_json,state FROM transfers").fetchall()
        write_keys(self.root, self.bbcp, self.data_port_first, self.data_port_last, rows)
