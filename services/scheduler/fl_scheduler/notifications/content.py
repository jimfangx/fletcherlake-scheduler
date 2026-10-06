"""Notifications expose a small fixed projection, never logs, configuration or raw errors."""

from enum import StrEnum
from uuid import UUID

from fl_common.models.base import Schema
from pydantic import AwareDatetime, Field


class EventName(StrEnum):
    JOB_SUCCEEDED = "JOB_SUCCEEDED"
    JOB_FAILED = "JOB_FAILED"
    JOB_TIMED_OUT = "JOB_TIMED_OUT"
    JOB_HANG_WARNING = "JOB_HANG_WARNING"
    JOB_CANCELED = "JOB_CANCELED"
    JOB_INTERRUPTED = "JOB_INTERRUPTED"
    JOB_LOST = "JOB_LOST"
    JOB_INTERRUPTED_BY_FORCE_POWEROFF = "JOB_INTERRUPTED_BY_FORCE_POWEROFF"
    CLUSTER_OFFLINE = "CLUSTER_OFFLINE"
    CLUSTER_ONLINE = "CLUSTER_ONLINE"
    ARTIFACT_EXPIRING = "ARTIFACT_EXPIRING"
    ARTIFACT_DELETED = "ARTIFACT_DELETED"


LABELS = {
    EventName.JOB_SUCCEEDED: "Job succeeded",
    EventName.JOB_FAILED: "Job failed",
    EventName.JOB_TIMED_OUT: "Job timed out",
    EventName.JOB_HANG_WARNING: "Possible job hang: execution deadline approaching",
    EventName.JOB_CANCELED: "Job canceled",
    EventName.JOB_INTERRUPTED: "Job interrupted",
    EventName.JOB_LOST: "Job lost",
    EventName.JOB_INTERRUPTED_BY_FORCE_POWEROFF: "Job interrupted by forced power-off",
    EventName.CLUSTER_OFFLINE: "Cluster offline",
    EventName.CLUSTER_ONLINE: "Cluster online",
    EventName.ARTIFACT_EXPIRING: "Job artifacts expire within 24 hours",
    EventName.ARTIFACT_DELETED: "Job artifacts deleted",
}


class Notice(Schema):
    event_id: int = Field(gt=0)
    event_type: EventName
    timestamp: AwareDatetime
    job_id: UUID | None = None
    cluster_id: UUID | None = None
    expires_at: AwareDatetime | None = None
    recipient: str | None = None

    def text(self, notification_id: UUID, origin: str) -> str:
        lines = [LABELS[self.event_type], "Event time: " + self.timestamp.isoformat()]
        if self.job_id:
            lines.extend(["Job: " + str(self.job_id), f"Details: {origin}/api/jobs/{self.job_id}"])
        elif self.cluster_id:
            lines.extend(
                [
                    "Cluster: " + str(self.cluster_id),
                    f"Details: {origin}/api/clusters/{self.cluster_id}",
                ]
            )
        if self.expires_at:
            lines.append("Expiry: " + self.expires_at.isoformat())
        lines.append("Notification: " + str(notification_id))
        return "\n".join(lines)
