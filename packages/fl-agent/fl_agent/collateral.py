"""UUID-scoped content storage and crash-safe, durable artifact deletion."""

import hashlib
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import ArtifactRecord, ArtifactRef, JobSpec
from fl_common.models.artifact import ARTIFACT_FILENAMES, ArtifactKind
from fl_common.models.base import utcnow

from .db import AgentDB
from .files import atomic_write, fsync_directory, sha256_file


class CollateralStore:
    def __init__(self, root: Path, db: AgentDB) -> None:
        self.root = root.resolve()
        self.db = db
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)

    def directory(self, job_id: UUID) -> Path:
        return self.root / str(job_id)

    def path(self, job_id: UUID, kind: ArtifactKind) -> Path:
        return self.directory(job_id) / ARTIFACT_FILENAMES[kind]

    def copy_input(self, job_id: UUID, kind: ArtifactKind, source: Path) -> ArtifactRef:
        """Hash copied bytes, preventing a changing source from bypassing integrity checks."""
        target = self.path(job_id, kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=target.parent, prefix=".staging-")
        digest = hashlib.sha256()
        size = 0
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                os.fchmod(output.fileno(), 0o600)
                while chunk := input_file.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            fsync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return ArtifactRef(
            kind=kind, sha256=digest.hexdigest(), size_bytes=size, original_name=source.name
        )

    def verify(self, spec: JobSpec) -> None:
        for ref in (spec.binary, spec.bitstream):
            if ref is not None:
                path = self.path(spec.job_id, ref.kind)
                if not path.is_file() or sha256_file(path) != (ref.sha256, ref.size_bytes):
                    raise PlatformError(
                        "ARTIFACT_INTEGRITY", f"Artifact verification failed: {ref.kind}"
                    )

    def register(self, job_id: UUID, ref: ArtifactRef) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO artifacts(job_id,kind,ref) VALUES (?,?,?) "
                "ON CONFLICT(job_id,kind) DO UPDATE SET ref=excluded.ref",
                (str(job_id), ref.kind, ref.model_dump_json()),
            )

    def save_spec(self, spec: JobSpec) -> None:
        atomic_write(self.path(spec.job_id, "job"), spec.model_dump_json(indent=2).encode())
        digest, size = sha256_file(self.path(spec.job_id, "job"))
        self.register(spec.job_id, ArtifactRef(kind="job", sha256=digest, size_bytes=size))
        for ref in (spec.binary, spec.bitstream):
            if ref:
                self.register(spec.job_id, ref)

    def finalize(self, spec: JobSpec, finished_at: datetime) -> None:
        """Retention starts at completion, so a long-running job cannot expire its own inputs."""
        for kind in ("job", "stdout", "stderr", "results", "binary", "bitstream"):
            path = self.path(spec.job_id, kind)
            registered = self.db.connection.execute(
                "SELECT 1 FROM artifacts WHERE job_id=? AND kind=?",
                (str(spec.job_id), kind),
            ).fetchone()
            if path.is_file() and (kind not in {"binary", "bitstream"} or not registered):
                sha256, size = sha256_file(path)
                self.register(spec.job_id, ArtifactRef(kind=kind, sha256=sha256, size_bytes=size))
        expires = (finished_at + timedelta(days=spec.collateral_ttl_days)).isoformat()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE artifacts SET expires_at=? WHERE job_id=? AND deleted_at IS NULL",
                (expires, str(spec.job_id)),
            )
            connection.execute(
                "UPDATE jobs SET retention_finalized=1 WHERE job_id=?",
                (str(spec.job_id),),
            )

    def records(self) -> list[ArtifactRecord]:
        return [
            ArtifactRecord(
                job_id=row["job_id"],
                ref=ArtifactRef.model_validate_json(row["ref"]),
                expires_at=row["expires_at"],
                deleted_at=row["deleted_at"],
            )
            for row in self.db.connection.execute("SELECT * FROM artifacts ORDER BY job_id,kind")
        ]

    def request_deletion(self, job_id: UUID) -> None:
        if not self.db.get(job_id).state.terminal:
            raise PlatformError("JOB_ACTIVE", "Cannot delete collateral for a nonterminal job")
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO collateral_deletion(job_id,state,requested_at) VALUES (?,'PENDING',?) "
                "ON CONFLICT(job_id) DO NOTHING",
                (str(job_id), utcnow().isoformat()),
            )

    def sweep(self, now: datetime | None = None) -> int:
        current = (now or utcnow()).isoformat()
        rows = self.db.connection.execute(
            "SELECT DISTINCT artifacts.job_id FROM artifacts JOIN jobs USING(job_id) "
            "WHERE deleted_at IS NULL AND retention_finalized=1 "
            "AND expires_at IS NOT NULL AND expires_at<=?",
            (current,),
        ).fetchall()
        for row in rows:
            self.request_deletion(UUID(row[0]))
        pending = self.db.connection.execute(
            "SELECT job_id FROM collateral_deletion WHERE state='PENDING'",
        ).fetchall()
        for row in pending:
            job_id = UUID(row[0])
            # A crash after removing files leaves a PENDING record. Repeating removal is safe.
            directory = self.directory(job_id)
            if directory.exists():
                shutil.rmtree(directory)
                fsync_directory(self.root)
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE artifacts SET deleted_at=? WHERE job_id=? AND deleted_at IS NULL",
                    (current, str(job_id)),
                )
                connection.execute(
                    "UPDATE collateral_deletion SET state='DONE',completed_at=? WHERE job_id=?",
                    (current, str(job_id)),
                )
                self.db._event(connection, str(job_id), "ARTIFACT_DELETED", None)
        return len(pending)
