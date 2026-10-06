"""Automatic retry budgets and explicit administrator redrive preserve delivery identity."""

import httpx
from fl_scheduler.auth.google import Identity
from fl_scheduler.db.models import Event, Notification
from fl_scheduler.notifications.service import Notifications
from sqlalchemy import select

from tests.notification_helpers import emit, make_due, settings, success


async def test_failure_budget_and_admin_retry_do_not_reset_cumulative_attempts_or_duplicate_intent(
    scheduler_db, auth_stack
):
    healthy = False

    def handle(request):
        return success(request) if healthy else httpx.Response(503, text="secret details")

    sessions, _, directory, provider, app = auth_stack
    alice = await sessions.issue(provider.identity)
    directory.members["admins"].add("admin@example.edu")
    admin = await sessions.issue(Identity("google-admin", "admin@example.edu"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        workflow = Notifications(
            scheduler_db, settings(("slack",), attempts=2), http, "https://scheduler.test"
        )
        event_id = emit(scheduler_db)
        await workflow.tick()
        make_due(scheduler_db)
        await workflow.tick()
        with scheduler_db.transaction() as session:
            row = session.scalar(select(Notification))
            assert row.state == "FAILED" and row.attempts == 2
            notification_id = row.notification_id
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://scheduler.test"
        ) as api:
            path = f"/api/admin/notifications/{notification_id}/retry"
            api.headers["Authorization"] = "Bearer " + alice.access_token.get_secret_value()
            assert (await api.get("/api/admin/notifications")).status_code == 403
            assert (await api.post(path)).status_code == 403
            api.headers["Authorization"] = "Bearer " + admin.access_token.get_secret_value()
            inspection = await api.get("/api/admin/notifications")
            assert inspection.status_code == 200 and "secret" not in inspection.text
            assert inspection.json()[0]["attempts"] == 2
            assert (await api.post(path)).status_code == 202
            assert (await api.post(path)).status_code == 202
            healthy = True
            make_due(scheduler_db)
            await workflow.tick()
            assert (
                await api.post(path)
            ).status_code == 202  # A lost response cannot resend a SENT row.
        with scheduler_db.transaction() as session:
            row = session.get(Notification, notification_id)
            assert row.state == "SENT" and row.attempts == 3 and row.budget_start == 2
            assert row.event_id == event_id
            assert (
                len(
                    list(
                        session.scalars(
                            select(Event).where(Event.type == "NOTIFICATION_RETRY_REQUESTED")
                        )
                    )
                )
                == 1
            )
