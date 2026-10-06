"""Delivery progress survives scheduler restart without storing any private identity."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fl_common.models.base import utcnow
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db.models import Base


class Delivery(Base):
    __tablename__ = "transfer_deliveries"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.job_id"), primary_key=True)
    transfer_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    source: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(32), default="STAGING")
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Upload(Base):
    __tablename__ = "job_uploads"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.job_id"), primary_key=True)
    upload_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    public_key: Mapped[str] = mapped_column(String(200), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    grant: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    next_cleanup_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
