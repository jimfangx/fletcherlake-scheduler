"""Safe provider outcomes and bounded Retry-After parsing."""

from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Literal

import httpx
from fl_common.models.base import utcnow


@dataclass(frozen=True)
class Outcome:
    state: Literal["SENT", "RETRY", "FAILED", "SKIPPED"]
    code: str | None = None
    status: int | None = None
    retry_after: float = 0


def retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - utcnow()).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, min(seconds, 86400))


def rejected(response: httpx.Response) -> Outcome | None:
    status = response.status_code
    if response.is_redirect:
        return Outcome("FAILED", "PROVIDER_REDIRECT", status)
    if response.is_success:
        return None
    transient = status in {408, 425, 429} or status >= 500
    return Outcome(
        "RETRY" if transient else "FAILED",
        f"PROVIDER_HTTP_{status}",
        status,
        retry_after(response.headers.get("Retry-After")) if transient else 0,
    )
