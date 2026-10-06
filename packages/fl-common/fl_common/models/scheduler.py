"""Scheduler contracts for identity, reservations, health, and agent snapshots."""

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, Field

from .artifact import ArtifactRecord
from .base import Schema
from .cluster import ClusterConfig, ClusterState
from .events import JobRecord
from .logs import LogHead


class Role(StrEnum):
    USER = "user"
    OPERATOR = "operator"
    ADMIN = "admin"


class Principal(Schema):
    email: str = Field(min_length=1)
    subject: str
    role: Role

    def permits(self, permission: str) -> bool:
        user = {"job:submit", "job:read", "cluster:read"}
        operator = user | {
            "job:manage",
            "cluster:drain",
            "cluster:restart",
            "cluster:force_poweroff",
        }
        admin = operator | {"cluster:enroll", "cluster:destroy", "system:admin"}
        return (
            permission in {Role.USER: user, Role.OPERATOR: operator, Role.ADMIN: admin}[self.role]
        )

    def owns(self, owner: str) -> bool:
        return self.email == owner or self.role in {Role.OPERATOR, Role.ADMIN}


class ClusterHealth(StrEnum):
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"


class AssignmentState(StrEnum):
    RESERVED = "RESERVED"
    STAGING = "STAGING"
    DELIVERING = "DELIVERING"
    QUEUED = "QUEUED"
    EXPIRED = "EXPIRED"
    FINISHED = "FINISHED"


class JobAssignment(Schema):
    job_id: UUID
    cluster_id: UUID
    board_id: str
    state: AssignmentState
    expires_at: AwareDatetime


class BoardStatus(Schema):
    board_id: str
    state: str
    active_job: UUID | None = None
    error: str | None = None


class SystemMetrics(Schema):
    cpu: float = Field(ge=0, le=1)
    memory: float = Field(ge=0, le=1)
    disk_free: int = Field(ge=0)
    disk_total: int = Field(ge=0)
    scheduler_connected: bool = False


class ClusterSnapshot(Schema):
    cluster: ClusterConfig
    state: ClusterState
    timestamp: AwareDatetime
    boards: list[BoardStatus]
    jobs: list[JobRecord]
    queues: dict[str, list[UUID]]
    artifacts: list[ArtifactRecord]
    last_event_sequence: int = Field(ge=0)
    system: SystemMetrics
    logs: list[LogHead] = Field(default_factory=list)
