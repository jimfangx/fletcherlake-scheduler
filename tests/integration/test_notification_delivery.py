"""Delivery leases, durable retry/pacing and shutdown recovery use real PostgreSQL."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from fl_common.models.base import utcnow
from fl_scheduler.db.models import Notification
from fl_scheduler.notifications.outcomes import Outcome
from fl_scheduler.notifications.service import Notifications
from sqlalchemy import select

from tests.notification_helpers import emit, make_due, settings, success


async def test_rate_limit_paces_all_rows_and_instances_for_the_same_webhook(scheduler_db):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, text="private response", headers={"Retry-After": "30"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        config = settings(("slack",))
        first = Notifications(scheduler_db, config, http, "https://scheduler.test")
        emit(scheduler_db)
        emit(scheduler_db, "JOB_FAILED")
        await first.tick()
        replacement = Notifications(scheduler_db, config, http, "https://scheduler.test")
        await replacement.tick()
        assert len(calls) == 1
        with scheduler_db.transaction() as session:
            rows = list(session.scalars(select(Notification)))
            assert {row.state for row in rows} == {"PENDING", "RETRY"}
            delayed = next(row for row in rows if row.state == "RETRY")
            assert delayed.next_attempt_at > utcnow() + timedelta(seconds=25)
            assert delayed.error == "PROVIDER_HTTP_429" and delayed.attempts == 1


async def test_expired_lease_replacement_fences_old_acks_and_concurrent_claims(scheduler_db):
    async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as http:
        workflow = Notifications(scheduler_db, settings(("slack",)), http, "https://scheduler.test")
        emit(scheduler_db)
        workflow.projector.project()
        with ThreadPoolExecutor(max_workers=4) as workers:
            claims = list(workers.map(lambda _: workflow.claims.claim(), range(4)))
        active = [claim for claim in claims if claim]
        assert len(active) == 1
        original = active[0]
        make_due(scheduler_db, expire_lease=True)
        assert workflow.claims.finish(original, Outcome("SENT")) is False
        renewed = workflow.claims.claim()
        assert (
            renewed.notification_id == original.notification_id
            and renewed.lease_id != original.lease_id
        )
        assert workflow.claims.finish(original, Outcome("FAILED", "STALE_RESULT")) is False
        await workflow.deliver(renewed)
        with scheduler_db.transaction() as session:
            row = session.get(Notification, original.notification_id)
            assert row.state == "SENT" and row.attempts == 2 and row.sent_at


@pytest.mark.parametrize("channel", ["slack", "google_chat", "mailgun"])
async def test_lost_provider_response_reuses_immutable_notification_identity(scheduler_db, channel):
    requests = []
    unique_messages = set()

    def handle(request):
        requests.append(request)
        if channel == "google_chat":
            unique_messages.add(request.url.params["requestId"])
        if len(requests) == 1:
            raise httpx.ReadError("Injected response loss after provider accepts")
        return success(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        config = settings((channel,))
        workflow = Notifications(scheduler_db, config, http, "https://scheduler.test")
        emit(scheduler_db)
        await workflow.tick()
        make_due(scheduler_db)
        replacement = Notifications(scheduler_db, config, http, "https://scheduler.test")
        await replacement.tick()
        assert len(requests) == 2
        if channel == "mailgun":
            # Multipart boundaries may differ; the stable correlation header is in both bodies.
            with scheduler_db.transaction() as session:
                identifier = str(session.scalar(select(Notification.notification_id)))
            assert all(identifier in request.content.decode() for request in requests)
        else:
            assert json.loads(requests[0].content) == json.loads(requests[1].content)
        if channel == "google_chat":
            assert len(unique_messages) == 1 and requests[0].url == requests[1].url
        with scheduler_db.transaction() as session:
            row = session.scalar(select(Notification))
            assert row.state == "SENT" and row.attempts == 2


async def test_worker_cancellation_reaps_io_and_preserves_claim_for_recovery(scheduler_db):
    entered = asyncio.Event()
    canceled = asyncio.Event()

    async def handle(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    config = settings(("slack",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        workflow = Notifications(scheduler_db, config, http, "https://scheduler.test")
        emit(scheduler_db)
        task = asyncio.create_task(workflow.tick())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert canceled.is_set()
        with scheduler_db.transaction() as session:
            assert session.scalar(select(Notification)).state == "SENDING"
    make_due(scheduler_db, expire_lease=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as http:
        replacement = Notifications(scheduler_db, config, http, "https://scheduler.test")
        await replacement.tick()
    with scheduler_db.transaction() as session:
        row = session.scalar(select(Notification))
        assert row.state == "SENT" and row.attempts == 2


async def test_removed_provider_is_skipped_without_rerouting_pending_intent(scheduler_db):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Removed route cannot send"))
    ) as http:
        original = Notifications(scheduler_db, settings(("slack",)), http, "https://scheduler.test")
        emit(scheduler_db)
        assert original.projector.project() == 1
        replacement = Notifications(scheduler_db, settings(()), http, "https://scheduler.test")
        await replacement.tick()
    with scheduler_db.transaction() as session:
        row = session.scalar(select(Notification))
        assert row.state == "SKIPPED" and row.error == "DESTINATION_REMOVED"
