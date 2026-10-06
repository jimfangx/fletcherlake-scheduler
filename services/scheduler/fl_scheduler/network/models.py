"""Durable bootstrap leases, encrypted delivery receipts, and network cleanup outbox."""

from datetime import datetime
from uuid import UUID, uuid4

from fl_common.models.base import utcnow
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db.models import Base


class Enrollment(Base):
    __tablename__ = "enrollments"
    enrollment_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    cluster_id: Mapped[UUID] = mapped_column(Uuid, unique=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(32), default="ISSUED")
    bootstrap_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NetworkRevocation(Base):
    __tablename__ = "network_revocations"
    revocation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    cluster_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clusters.cluster_id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    object_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finish_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (UniqueConstraint("kind", "object_id", name="network_revocation_identity"),)
