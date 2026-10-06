"""Notification fixtures use intercepted HTTP only; no external messages are sent."""

from datetime import timedelta

import httpx
from fl_common.models.base import utcnow
from fl_scheduler.db.models import Event
from fl_scheduler.notifications.config import MailgunConfig, NotificationSettings, WebhookConfig
from pydantic import SecretStr

SLACK = "https://hooks.slack.com/services/TTEST/BTEST/slack-secret"
CHAT = "https://chat.googleapis.com/v1/spaces/SPACE/messages?key=chat-key&token=chat-secret"
MAILGUN_KEY = "mailgun-secret"


def settings(channels=("mailgun", "slack", "google_chat"), *, backfill=True, attempts=8):
    return NotificationSettings(
        mailgun=MailgunConfig(
            api_key=SecretStr(MAILGUN_KEY),
            domain="mail.example.edu",
            sender="bringup@example.edu",
            operators=["operator@example.edu"],
        )
        if "mailgun" in channels
        else None,
        webhooks=[
            WebhookConfig(channel=channel, url=SecretStr(SLACK if channel == "slack" else CHAT))
            for channel in channels
            if channel != "mailgun"
        ],
        backfill_since=utcnow() - timedelta(days=2) if backfill else None,
        max_attempts=attempts,
    )


def success(request):
    if request.url.host.startswith("hooks.slack"):
        return httpx.Response(200, text="ok")
    if request.url.host == "chat.googleapis.com":
        return httpx.Response(
            200, json={"name": request.url.path.removeprefix("/v1/") + "/message"}
        )
    return httpx.Response(200, json={"id": "<mailgun-message>"})


def emit(db, name="JOB_SUCCEEDED", *, job_id=None, **values):
    with db.transaction() as session:
        event = Event(
            type=name, job_id=job_id, payload={"error": "untrusted secret-bearing error"}, **values
        )
        session.add(event)
        session.flush()
        return event.event_id


def make_due(db, *, expire_lease=False):
    from fl_scheduler.db.models import Notification
    from fl_scheduler.notifications.models import Route
    from sqlalchemy import select

    with db.transaction() as session:
        for row in session.scalars(select(Notification)):
            row.next_attempt_at = utcnow() - timedelta(seconds=1)
            if expire_lease and row.lease_until:
                row.lease_until = utcnow() - timedelta(seconds=1)
        for route in session.scalars(select(Route)):
            route.next_send_at = utcnow() - timedelta(seconds=1)
