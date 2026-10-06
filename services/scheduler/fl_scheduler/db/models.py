"""Scheduler metadata and replicated state. Human allowlisting belongs to Google Groups."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fl_common.models.base import utcnow
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), unique=True)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Cluster(Base):
    __tablename__ = "clusters"
    cluster_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    # Optional power_control is persisted in config; separate column supports future indexing.
    power_control: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(64), default="CONFIGURATION_INCOMPLETE")
    desired_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    health: Mapped[str] = mapped_column(String(16), default="OFFLINE")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    agent_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    headscale_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    headscale_addresses: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    current_session: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)


class Board(Base):
    __tablename__ = "boards"
    board_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.cluster_id"), index=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(64), default="IDLE")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    active_job: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    session_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.cluster_id"), index=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    job_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    owner: Mapped[str] = mapped_column(String(320), index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(64), default="CREATED", index=True)
    priority: Mapped[int] = mapped_column(Integer)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    record: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    submission_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    submission_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        Index("jobs_priority_fifo", "priority", "submitted_at", "job_id"),
        UniqueConstraint("owner", "submission_id", name="job_submission_identity"),
    )


class Assignment(Base):
    __tablename__ = "job_assignments"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.job_id"), primary_key=True)
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.cluster_id"), index=True)
    board_id: Mapped[str] = mapped_column(ForeignKey("boards.board_id"), index=True)
    state: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # PostgreSQL enforces one in-flight transfer reservation per board across scheduler instances.
    __table_args__ = (
        Index(
            "one_transfer_per_board",
            "board_id",
            unique=True,
            postgresql_where=state.in_(["RESERVED", "STAGING"]),
        ),
    )


class Artifact(Base):
    __tablename__ = "artifacts"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.job_id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class Event(Base):
    __tablename__ = "events"
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clusters.cluster_id"), nullable=True
    )
    agent_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("jobs.job_id"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("agent_event_identity", "cluster_id", "agent_sequence", unique=True),)


class Notification(Base):
    __tablename__ = "notifications"
    notification_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.event_id"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    budget_start: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_id: Mapped[str | None] = mapped_column(
        ForeignKey("notification_routes.route_id"), nullable=True
    )
    destination_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    lease_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "event_id", "route_id", "destination_hash", name="notification_delivery_identity"
        ),
        Index("notification_due", "state", "next_attempt_at"),
    )


class Command(Base):
    __tablename__ = "agent_commands"
    message_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.cluster_id"), index=True)
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.job_id"), nullable=True)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
