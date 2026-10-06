"""Provider-specific wire formats; URL/key secrets stay only in protected configuration."""

import hashlib
import json
from typing import Protocol
from urllib.parse import parse_qsl
from uuid import UUID

import httpx

from .config import MailgunConfig, NotificationSettings, WebhookConfig
from .content import LABELS, Notice
from .outcomes import Outcome, rejected


def identity(parts: list[str]) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


class Provider(Protocol):
    channel: str
    route_id: str

    async def send(self, notification_id: UUID, notice: Notice, origin: str) -> Outcome: ...


class Mailgun:
    channel = "mailgun"

    def __init__(self, config: MailgunConfig, client: httpx.AsyncClient) -> None:
        self.config, self.client = config, client
        self.route_id = identity(
            [
                self.channel,
                config.region,
                config.domain,
                config.sender,
            ]
        )

    async def send(self, notification_id: UUID, notice: Notice, origin: str) -> Outcome:
        if notice.recipient is None:
            return Outcome("SKIPPED", "NO_RECIPIENT")
        host = "api.mailgun.net" if self.config.region == "us" else "api.eu.mailgun.net"
        fields = {
            "from": self.config.sender,
            "to": notice.recipient,
            "subject": LABELS[notice.event_type],
            "text": notice.text(notification_id, origin),
            "h:Message-Id": f"<fl-{notification_id}@{self.config.domain}>",
        }
        response = await self.client.post(
            f"https://{host}/v3/{self.config.domain}/messages",
            auth=("api", self.config.api_key.get_secret_value()),
            files={key: (None, value) for key, value in fields.items()},
            timeout=15,
            follow_redirects=False,
        )
        if failure := rejected(response):
            return failure
        try:
            valid = isinstance(response.json().get("id"), str) and bool(response.json()["id"])
        except (ValueError, AttributeError, KeyError):
            valid = False
        return (
            Outcome("SENT", status=response.status_code)
            if valid
            else Outcome("RETRY", "INVALID_ACK", response.status_code)
        )


class Webhook:
    def __init__(self, config: WebhookConfig, client: httpx.AsyncClient) -> None:
        self.config, self.client = config, client
        self.channel: str = config.channel
        endpoint = httpx.URL(config.url.get_secret_value())
        # Tokens may rotate while the logical destination remains the same.
        path = endpoint.path.rsplit("/", 1)[0] if self.channel == "slack" else endpoint.path
        self.route_id = identity([self.channel, endpoint.host, path])

    async def send(self, notification_id: UUID, notice: Notice, origin: str) -> Outcome:
        text = notice.text(notification_id, origin)
        endpoint = httpx.URL(self.config.url.get_secret_value())
        if self.channel == "slack":
            body = {
                "text": text,
                "unfurl_links": False,
                "unfurl_media": False,
                "blocks": [{"type": "section", "text": {"type": "plain_text", "text": text}}],
            }
        else:
            body = {"text": text}
            query = dict(parse_qsl(endpoint.query.decode()))
            query["requestId"] = str(notification_id)
            endpoint = endpoint.copy_with(params=query)
        response = await self.client.post(endpoint, json=body, timeout=15, follow_redirects=False)
        if failure := rejected(response):
            return failure
        if self.channel == "slack":
            valid = response.text.strip() == "ok"
        else:
            try:
                name = response.json().get("name")
                valid = isinstance(name, str) and name.startswith(
                    endpoint.path.removeprefix("/v1/") + "/"
                )
            except (ValueError, AttributeError):
                valid = False
        return (
            Outcome("SENT", status=response.status_code)
            if valid
            else Outcome("RETRY", "INVALID_ACK", response.status_code)
        )


def providers(settings: NotificationSettings, client: httpx.AsyncClient) -> dict[str, Provider]:
    configured: list[Provider] = [Webhook(config, client) for config in settings.webhooks]
    if settings.mailgun:
        configured.append(Mailgun(settings.mailgun, client))
    return {provider.route_id: provider for provider in configured}
