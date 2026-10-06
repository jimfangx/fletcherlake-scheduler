"""Atomically route events without assuming sequence IDs commit in allocation order."""

from collections.abc import Mapping

from fl_common.models.base import utcnow
from pydantic import AwareDatetime, TypeAdapter
from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert

from ..db.core import Database
from ..db.models import Event, Job, Notification, User
from .config import NotificationSettings, email_address
from .content import EventName, Notice
from .models import Projection, Route
from .providers import Provider, identity


def expiration(value: object) -> AwareDatetime | None:
    try:
        return TypeAdapter(AwareDatetime).validate_python(value)
    except ValueError:
        return None


class Projector:
    def __init__(
        self, db: Database, settings: NotificationSettings, providers: Mapping[str, Provider]
    ) -> None:
        self.db, self.settings, self.providers = db, settings, providers
        self.activated_at = utcnow()

    def project(self, limit: int = 100) -> int:
        added = 0
        with self.db.transaction() as session:
            for provider in sorted(self.providers.values(), key=lambda provider: provider.route_id):
                session.execute(
                    insert(Route)
                    .values(
                        route_id=provider.route_id,
                        channel=provider.channel,
                        accept_after=self.settings.backfill_since or self.activated_at,
                        next_send_at=utcnow(),
                    )
                    .on_conflict_do_nothing()
                )
                route = session.get(Route, provider.route_id)
                assert route is not None
                if (
                    self.settings.backfill_since
                    and self.settings.backfill_since < route.accept_after
                ):
                    route.accept_after = self.settings.backfill_since
                    session.flush()
                projected = exists().where(
                    Projection.route_id == route.route_id, Projection.event_id == Event.event_id
                )
                events = list(
                    session.scalars(
                        select(Event)
                        .where(
                            Event.type.in_(list(EventName)),
                            Event.ingested_at >= route.accept_after,
                            ~projected,
                        )
                        .order_by(Event.event_id)
                        .limit(limit)
                    )
                )
                for event in events:
                    claimed = session.scalar(
                        insert(Projection)
                        .values(route_id=route.route_id, event_id=event.event_id)
                        .on_conflict_do_nothing()
                        .returning(Projection.event_id)
                    )
                    if claimed is None:
                        continue
                    notice = Notice(
                        event_id=event.event_id,
                        event_type=EventName(event.type),
                        timestamp=event.timestamp,
                        job_id=event.job_id,
                        cluster_id=event.cluster_id,
                        expires_at=expiration(event.payload.get("expires_at"))
                        if event.type == "ARTIFACT_EXPIRING"
                        else None,
                    )
                    recipients: list[str | None] = [None]
                    if provider.channel == "mailgun":
                        assert self.settings.mailgun is not None
                        recipients = list(self.settings.mailgun.operators)
                        job = session.get(Job, event.job_id) if event.job_id else None
                        if (
                            self.settings.mailgun.include_owner
                            and job
                            and session.get(User, job.owner)
                        ):
                            try:
                                recipients.append(email_address(job.owner))
                            except ValueError:
                                pass
                    for recipient in sorted(set(recipients), key=lambda value: value or ""):
                        payload = notice.model_copy(update={"recipient": recipient}).model_dump(
                            mode="json"
                        )
                        session.add(
                            Notification(
                                event_id=event.event_id,
                                channel=provider.channel,
                                route_id=route.route_id,
                                destination_hash=identity([route.route_id, recipient or "webhook"]),
                                payload=payload,
                            )
                        )
                        added += 1
        return added
