"""Export progress and public download identities survive scheduler restarts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fl_common.models.base import utcnow
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..db.models import Base


class Export(Base):
    __tablename__ = "artifact_exports"
    export_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    manifest_hash: Mapped[str] = mapped_column(String(64), index=True)
    artifacts: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    grant: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="PREPARING")
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Download(Base):
    __tablename__ = "artifact_downloads"
    download_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.job_id"))
    export_id: Mapped[UUID] = mapped_column(ForeignKey("artifact_exports.export_id"), index=True)
    owner: Mapped[str] = mapped_column(String(320))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    request_hash: Mapped[str] = mapped_column(String(64))
    public_key: Mapped[str] = mapped_column(String(200), unique=True)
    grant: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("owner", "request_id", name="download_request_identity"),)
