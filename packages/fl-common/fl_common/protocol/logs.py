"""Read commands expire quickly and can address only stdout/stderr at a fixed job path."""

from pydantic import AwareDatetime, Field, model_validator

from fl_common.models.base import Schema
from fl_common.models.logs import MAX_LOG_BYTES, MAX_OFFSET, LogHead


class LogRead(Schema):
    head: LogHead
    offset: int = Field(ge=0, le=MAX_OFFSET)
    byte_limit: int = Field(default=MAX_LOG_BYTES, ge=1, le=MAX_LOG_BYTES)
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def range(self) -> "LogRead":
        if self.offset > self.head.size_bytes:
            raise ValueError("Log offset exceeds the advertised file size")
        return self
