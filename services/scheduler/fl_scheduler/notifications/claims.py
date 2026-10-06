"""Leases fence stale workers; route locks pace all instances of a provider destination."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from fl_common.models.base import utcnow
from sqlalchemy import and_, or_, select

from ..db.core import Database
from ..db.models import Notification
from .content import Notice
from .models import Route
from .outcomes import Outcome


@dataclass(frozen=True)
class Delivery:
    notification_id: UUID
    route_id: str
    lease_id: UUID
    notice: Notice


class Claims:
    def __init__(self, db: Database, max_attempts: int) -> None:
        self.db, self.max_attempts = db, max_attempts

    def claim(self) -> Delivery | None:
        now = utcnow()
        with self.db.transaction() as session:
            candidates = session.scalars(
                select(Notification)
                .where(
                    or_(
                        and_(
                            Notification.state.in_(["PENDING", "RETRY"]),
                            Notification.next_attempt_at <= now,
                        ),
                        and_(Notification.state == "SENDING", Notification.lease_until <= now),
                    )
                )
                .order_by(Notification.next_attempt_at, Notification.notification_id)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
            for row in candidates:
                if row.route_id is None or row.payload is None:
                    row.state, row.error = "SKIPPED", "LEGACY_UNROUTED"
                    continue
                if row.attempts - row.budget_start >= self.max_attempts:
                    row.state, row.error = "FAILED", "ATTEMPT_BUDGET_EXHAUSTED"
                    row.lease_id = row.lease_until = None
                    continue
                route = session.scalar(
                    select(Route)
                    .where(Route.route_id == row.route_id)
                    .with_for_update(skip_locked=True)
                )
                if route is None or route.next_send_at > now:
                    continue
                route.next_send_at = now + timedelta(seconds=1)
                row.state, row.lease_id = "SENDING", uuid4()
                row.lease_until = now + timedelta(seconds=60)
                row.attempts += 1
                return Delivery(
                    row.notification_id,
                    row.route_id,
                    row.lease_id,
                    Notice.model_validate(row.payload),
                )
        return None

    def finish(self, delivery: Delivery, outcome: Outcome) -> bool:
        with self.db.transaction() as session:
            row = session.get(Notification, delivery.notification_id, with_for_update=True)
            if (
                row is None
                or row.state != "SENDING"
                or row.lease_id != delivery.lease_id
                or row.lease_until is None
                or row.lease_until <= utcnow()
            ):
                return False
            row.state, row.error, row.last_status = outcome.state, outcome.code, outcome.status
            row.lease_id = row.lease_until = None
            if outcome.state == "SENT":
                row.sent_at = utcnow()
            elif outcome.state == "RETRY":
                if row.attempts - row.budget_start >= self.max_attempts:
                    row.state, row.error = "FAILED", "ATTEMPT_BUDGET_EXHAUSTED"
                else:
                    delay = max(
                        min(2 ** (row.attempts - row.budget_start), 300), outcome.retry_after
                    )
                    row.next_attempt_at = utcnow() + timedelta(seconds=delay)
                if outcome.retry_after:
                    route = session.get(Route, delivery.route_id, with_for_update=True)
                    assert route is not None
                    route.next_send_at = max(
                        route.next_send_at, utcnow() + timedelta(seconds=outcome.retry_after)
                    )
            return True
