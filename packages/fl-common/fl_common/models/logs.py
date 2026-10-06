"""Bounded, byte-oriented log pages and append-only file watermarks."""

import base64
import hashlib
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .base import Schema

LogStream = Literal["stdout", "stderr"]
MAX_LOG_BYTES = 64 * 1024
MAX_OFFSET = (1 << 63) - 1


class LogHead(Schema):
    job_id: UUID
    stream: LogStream
    file_id: str | None = Field(default=None, max_length=64)
    size_bytes: int = Field(ge=0, le=MAX_OFFSET)
    terminal: bool
    retained: bool
    expires_at: AwareDatetime | None = None


class LogChunk(Schema):
    job_id: UUID
    stream: LogStream
    file_id: str | None = None
    offset: int = Field(ge=0, le=MAX_OFFSET)
    next_offset: int = Field(ge=0, le=MAX_OFFSET)
    data_b64: str = Field(max_length=4 * ((MAX_LOG_BYTES + 2) // 3))
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eof: bool

    def data(self) -> bytes:
        return base64.b64decode(self.data_b64, validate=True)

    @model_validator(mode="after")
    def integrity(self) -> "LogChunk":
        data = self.data()
        if len(data) > MAX_LOG_BYTES or self.next_offset != self.offset + len(data):
            raise ValueError("Log chunk exceeds its byte range")
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError("Log chunk failed SHA verification")
        return self

    @classmethod
    def build(cls, head: LogHead, offset: int, data: bytes) -> "LogChunk":
        return cls(
            job_id=head.job_id,
            stream=head.stream,
            file_id=head.file_id,
            offset=offset,
            next_offset=offset + len(data),
            data_b64=base64.b64encode(data).decode(),
            sha256=hashlib.sha256(data).hexdigest(),
            eof=head.terminal and offset + len(data) == head.size_bytes,
        )


class LogPage(Schema):
    job_id: UUID
    stream: LogStream
    offset: int = Field(ge=0, le=MAX_OFFSET)
    state: Literal["WAITING", "READY"]
    head: LogHead | None = None
    chunk: LogChunk | None = None

    @model_validator(mode="after")
    def scope(self) -> "LogPage":
        if self.head and (self.head.job_id, self.head.stream) != (self.job_id, self.stream):
            raise ValueError("Log head belongs to another stream")
        if (self.chunk is not None) != (self.state == "READY"):
            raise ValueError("Only ready pages contain bytes")
        if self.chunk and (
            (self.chunk.job_id, self.chunk.stream, self.chunk.offset)
            != (self.job_id, self.stream, self.offset)
            or self.head is None
            or self.chunk.file_id != self.head.file_id
            or self.chunk.next_offset > self.head.size_bytes
            or self.chunk.eof
            != (self.head.terminal and self.chunk.next_offset == self.head.size_bytes)
            or (self.chunk.next_offset == self.offset and self.offset != self.head.size_bytes)
        ):
            raise ValueError("Log bytes differ from the requested stream")
        return self
