"""Versioned command envelopes; ACKs always name the original message ID."""

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from fl_common.models.base import Schema, utcnow


class MessageType(StrEnum):
    HELLO = "HELLO"
    REGISTER_CLUSTER = "REGISTER_CLUSTER"
    REGISTER_ACK = "REGISTER_ACK"
    HEARTBEAT = "HEARTBEAT"
    STATUS_SNAPSHOT = "STATUS_SNAPSHOT"
    STATUS_REQUEST = "STATUS_REQUEST"
    JOB_STAGE = "JOB_STAGE"
    JOB_FETCH = "JOB_FETCH"
    JOB_ENQUEUE = "JOB_ENQUEUE"
    JOB_CANCEL = "JOB_CANCEL"
    JOB_QUEUED = "JOB_QUEUED"
    JOB_STARTED = "JOB_STARTED"
    JOB_LOG = "JOB_LOG"
    LOG_READ = "LOG_READ"
    JOB_FINISHED = "JOB_FINISHED"
    JOB_FAILED = "JOB_FAILED"
    ARTIFACT_EXPIRING = "ARTIFACT_EXPIRING"
    ARTIFACT_DELETED = "ARTIFACT_DELETED"
    ARTIFACT_DELETE = "ARTIFACT_DELETE"
    ARTIFACT_PREPARE = "ARTIFACT_PREPARE"
    ARTIFACT_PUBLISH = "ARTIFACT_PUBLISH"
    CLUSTER_DRAIN = "CLUSTER_DRAIN"
    CLUSTER_RESTARTING = "CLUSTER_RESTARTING"
    CLUSTER_RECONFIGURING = "CLUSTER_RECONFIGURING"
    CLUSTER_DESTROYING = "CLUSTER_DESTROYING"
    ACK = "ACK"


class Message(Schema):
    protocol_version: Literal[1] = 1
    message_id: UUID = Field(default_factory=uuid4)
    timestamp: AwareDatetime = Field(default_factory=utcnow)
    type: MessageType
    payload: dict[str, Any] = Field(default_factory=dict)


class Ack(Schema):
    message_id: UUID
    accepted: bool
    error: str | None = None
    error_code: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
