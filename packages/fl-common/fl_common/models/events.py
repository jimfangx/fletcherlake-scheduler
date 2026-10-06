"""Durable, ordered events and observable job records."""

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, Field

from .base import Schema, utcnow
from .job import JobSpec


class JobState(StrEnum):
    CREATED = "CREATED"
    STAGING = "STAGING"
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    PROGRAMMING_FPGA = "PROGRAMMING_FPGA"
    PROGRAMMING_SOC = "PROGRAMMING_SOC"
    RUNNING = "RUNNING"
    CANCELING = "CANCELING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELED = "CANCELED"
    INTERRUPTED = "INTERRUPTED"
    LOST = "LOST"
    INTERRUPTED_BY_FORCE_POWEROFF = "INTERRUPTED_BY_FORCE_POWEROFF"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATES


TERMINAL_STATES = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.TIMED_OUT,
        JobState.CANCELED,
        JobState.INTERRUPTED,
        JobState.LOST,
        JobState.INTERRUPTED_BY_FORCE_POWEROFF,
    }
)
ACTIVE_STATES = frozenset(
    {
        JobState.PREPARING,
        JobState.PROGRAMMING_FPGA,
        JobState.PROGRAMMING_SOC,
        JobState.RUNNING,
        JobState.CANCELING,
    }
)


class JobEvent(Schema):
    seq: int
    job_id: UUID | None = None
    type: str
    timestamp: AwareDatetime = Field(default_factory=utcnow)
    board_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class JobRecord(Schema):
    spec: JobSpec
    board_id: str
    state: JobState
    updated_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error: str | None = None
