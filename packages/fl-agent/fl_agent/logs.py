"""Read fixed log files without delaying UART execution or trusting caller-selected paths."""

import os
import stat
from datetime import timedelta
from uuid import UUID

from fl_common.async_calls import background_call
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_common.models.logs import LogChunk, LogHead, LogStream
from fl_common.protocol.logs import LogRead

from .collateral import CollateralStore
from .db import AgentDB


class AgentLogs:
    def __init__(self, db: AgentDB, store: CollateralStore) -> None:
        self.db, self.store = db, store

    def head(self, job_id: UUID, stream: LogStream) -> LogHead:
        job = self.db.get(job_id)
        expires = (
            job.finished_at + timedelta(days=job.spec.collateral_ttl_days)
            if job.finished_at
            else None
        )
        deleted = self.db.connection.execute(
            "SELECT 1 FROM collateral_deletion WHERE job_id=?", (str(job_id),)
        ).fetchone()
        retained = not deleted and (expires is None or expires > utcnow())
        result = LogHead(
            job_id=job_id,
            stream=stream,
            size_bytes=0,
            terminal=job.state.terminal,
            retained=bool(retained),
            expires_at=expires,
        )
        if retained:
            try:
                info = self.store.path(job_id, stream).lstat()
            except FileNotFoundError:
                return result
            if not stat.S_ISREG(info.st_mode):
                result.retained = False
            else:
                result.file_id, result.size_bytes = str(info.st_ino), info.st_size
        return result

    def heads(self) -> list[dict[str, object]]:
        streams: tuple[LogStream, ...] = ("stdout", "stderr")
        return [
            self.head(job.spec.job_id, stream).model_dump(mode="json")
            for job in self.db.jobs()
            for stream in streams
        ]

    async def read(self, request: LogRead) -> dict[str, object]:
        current = self.head(request.head.job_id, request.head.stream)
        self.check(request, current)
        data = await background_call(self.read_bytes, request)
        self.check(request, self.head(current.job_id, current.stream))
        return LogChunk.build(request.head, request.offset, data).model_dump(mode="json")

    @staticmethod
    def check(request: LogRead, current: LogHead) -> None:
        if request.expires_at <= utcnow():
            raise PlatformError("LOG_REQUEST_EXPIRED", "Log read deadline expired")
        if not current.retained:
            raise PlatformError(
                "ARTIFACT_EXPIRED", "Log retention expired or deletion was requested"
            )
        if current.file_id != request.head.file_id or current.size_bytes < request.head.size_bytes:
            raise PlatformError("LOG_CHANGED", "Log file changed; refresh its watermark")

    def read_bytes(self, request: LogRead) -> bytes:
        if request.head.file_id is None:
            return b""
        descriptor = os.open(
            self.store.path(request.head.job_id, request.head.stream),
            # POSIX fsync requires a writable descriptor (including on macOS).
            os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or str(info.st_ino) != request.head.file_id:
                raise PlatformError("LOG_CHANGED", "Log path no longer names the advertised file")
            os.fsync(descriptor)
            os.lseek(descriptor, request.offset, os.SEEK_SET)
            data = os.read(
                descriptor, min(request.byte_limit, request.head.size_bytes - request.offset)
            )
            if len(data) != min(request.byte_limit, request.head.size_bytes - request.offset):
                raise PlatformError("LOG_CHANGED", "Advertised log bytes are no longer present")
            return data
        finally:
            os.close(descriptor)
