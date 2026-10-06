"""Protected service configuration; provider secrets never enter jobs or PostgreSQL."""

import os
import re
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from fl_common.models.base import Schema
from pydantic import AwareDatetime, Field, SecretStr, field_validator


def email_address(value: str) -> str:
    if len(value) > 320 or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", value
    ):
        raise ValueError("Invalid notification email address")
    return value.lower()


class MailgunConfig(Schema):
    api_key: SecretStr
    domain: str = Field(pattern=r"^[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?$")
    sender: str
    region: Literal["us", "eu"] = "us"
    operators: list[str] = Field(default_factory=list, max_length=100)
    include_owner: bool = True

    @field_validator("sender")
    @classmethod
    def sender_email(cls, value: str) -> str:
        return email_address(value)

    @field_validator("operators")
    @classmethod
    def operator_emails(cls, values: list[str]) -> list[str]:
        return sorted({email_address(value) for value in values})

    @field_validator("api_key")
    @classmethod
    def nonempty_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("Mailgun API key is empty")
        return value


class WebhookConfig(Schema):
    channel: Literal["slack", "google_chat"]
    url: SecretStr

    @field_validator("url")
    @classmethod
    def https_url(cls, value: SecretStr) -> SecretStr:
        url = urlsplit(value.get_secret_value())
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.fragment
            or url.port not in {None, 443}
        ):
            raise ValueError("Webhook must use provider HTTPS without userinfo or fragments")
        return value


class NotificationSettings(Schema):
    mailgun: MailgunConfig | None = None
    webhooks: list[WebhookConfig] = Field(default_factory=list, max_length=2)
    backfill_since: AwareDatetime | None = None
    max_attempts: int = Field(default=8, ge=1, le=20)

    @field_validator("webhooks")
    @classmethod
    def provider_urls(cls, values: list[WebhookConfig]) -> list[WebhookConfig]:
        if len({value.channel for value in values}) != len(values):
            raise ValueError("Configure at most one webhook per provider")
        for value in values:
            url = urlsplit(value.url.get_secret_value())
            if value.channel == "slack":
                if (
                    url.hostname not in {"hooks.slack.com", "hooks.slack-gov.com"}
                    or url.query
                    or not re.fullmatch(
                        r"/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9_-]+", url.path
                    )
                ):
                    raise ValueError("Invalid Slack incoming webhook")
            else:
                query = parse_qs(url.query, keep_blank_values=True)
                if (
                    url.hostname != "chat.googleapis.com"
                    or not re.fullmatch(r"/v1/spaces/[A-Za-z0-9_-]+/messages", url.path)
                    or set(query) != {"key", "token"}
                    or any(len(parts) != 1 or not parts[0] for parts in query.values())
                ):
                    raise ValueError("Invalid Google Chat incoming webhook")
        return values


def load_settings(path: Path) -> NotificationSettings:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 65536
        ):
            raise RuntimeError("Notification settings require an owned, protected regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(65537)
        try:
            return NotificationSettings.model_validate_json(content)
        except ValueError:
            # Pydantic errors include input values; never expose webhook URLs or API keys.
            raise RuntimeError("Invalid notification configuration") from None
    finally:
        os.close(descriptor)
