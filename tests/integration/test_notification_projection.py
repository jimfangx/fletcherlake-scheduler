"""Transactional fanout covers concurrent workers, late commits and activation boundaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
from fl_common.models import JobConfig
from fl_common.models.base import utcnow
from fl_scheduler.db.models import Event, Notification, User
from fl_scheduler.notifications.models import Projection
from fl_scheduler.notifications.service import Notifications
from fl_scheduler.scheduler.placement import Placement
from sqlalchemy import select

from tests.integration.test_artifact_downloads import OWNER
from tests.notification_helpers import emit, settings


def test_concurrent_projection_routes_once_and_excludes_raw_errors_and_provider_secrets(
    scheduler_db,
):
    with scheduler_db.transaction() as session:
        session.add(User(email=OWNER.email, subject=OWNER.subject))
    spec = Placement(scheduler_db).submit(JobConfig(), OWNER)
    client = httpx.AsyncClient()
    workflow = Notifications(scheduler_db, settings(), client, "https://scheduler.test")
    names = [
        "JOB_SUCCEEDED",
        "JOB_FAILED",
        "JOB_TIMED_OUT",
        "JOB_HANG_WARNING",
        "ARTIFACT_EXPIRING",
        "ARTIFACT_DELETED",
    ]
    for name in names:
        emit(scheduler_db, name, job_id=spec.job_id)
    emit(scheduler_db, "JOB_LOG", job_id=spec.job_id)
    with ThreadPoolExecutor(max_workers=4) as workers:
        counts = list(workers.map(lambda _: workflow.projector.project(), range(4)))
    assert sum(counts) == len(names) * 4  # Owner email, operator email, Slack and Chat.
    with scheduler_db.transaction() as session:
        rows = list(session.scalars(select(Notification)))
        assert len(rows) == len(names) * 4
        assert len(list(session.scalars(select(Projection)))) == len(names) * 3
        serialized = str([row.payload for row in rows])
        for secret in (
            "untrusted secret-bearing error",
            "mailgun-secret",
            "slack-secret",
            "chat-secret",
        ):
            assert secret not in serialized
        assert {row.payload["recipient"] for row in rows if row.channel == "mailgun"} == {
            OWNER.email,
            "operator@example.edu",
        }
    assert workflow.projector.project() == 0


def test_projection_catches_lower_sequence_id_that_commits_after_a_higher_one(scheduler_db):
    workflow = Notifications(
        scheduler_db, settings(("slack",)), httpx.AsyncClient(), "https://scheduler.test"
    )
    transaction = scheduler_db.sessions()
    try:
        transaction.begin()
        late = Event(type="JOB_SUCCEEDED", payload={})
        transaction.add(late)
        transaction.flush()
        high = emit(scheduler_db)
        assert high > late.event_id
        assert workflow.projector.project() == 1
        transaction.commit()
        assert workflow.projector.project() == 1
        with scheduler_db.transaction() as session:
            assert {row.event_id for row in session.scalars(select(Notification))} == {
                late.event_id,
                high,
            }
    finally:
        transaction.close()


def test_route_activation_is_durable_and_uses_ingestion_time_despite_mac_clock_skew(scheduler_db):
    old = emit(scheduler_db, ingested_at=utcnow() - timedelta(days=1))
    client = httpx.AsyncClient()
    config = settings(("slack",), backfill=False)
    workflow = Notifications(scheduler_db, config, client, "https://scheduler.test")
    current = emit(scheduler_db, timestamp=utcnow() - timedelta(days=7))
    assert workflow.projector.project() == 1
    next_event = emit(scheduler_db)
    replacement = Notifications(scheduler_db, config, client, "https://scheduler.test")
    assert replacement.projector.project() == 1
    with scheduler_db.transaction() as session:
        assert {row.event_id for row in session.scalars(select(Notification))} == {
            current,
            next_event,
        }
    backfill = Notifications(scheduler_db, settings(("slack",)), client, "https://scheduler.test")
    assert backfill.projector.project() == 1
    with scheduler_db.transaction() as session:
        assert {row.event_id for row in session.scalars(select(Notification))} == {
            old,
            current,
            next_event,
        }
