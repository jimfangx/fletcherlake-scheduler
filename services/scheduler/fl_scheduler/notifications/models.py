"""Projection identities and provider pacing survive worker replacement."""

from datetime import datetime

from fl_common.models.base import utcnow
from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db.models import Base


class Route(Base):
    __tablename__ = "notification_routes"
    route_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel: Mapped[str] = mapped_column(String(32))
    accept_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Projection(Base):
    __tablename__ = "notification_projections"
    route_id: Mapped[str] = mapped_column(
        ForeignKey("notification_routes.route_id"), primary_key=True
    )
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.event_id"), primary_key=True, index=True
    )


class AlertMarker(Base):
    __tablename__ = "notification_alert_markers"
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.event_id"), index=True)
