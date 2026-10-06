"""Terminal approval without local browser callbacks, root, or private-network access."""

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from fl_common.errors import PlatformError

from .api import checked
from .credentials import Credentials, CredentialStore, https_origin


def login(
    scheduler: str,
    store: CredentialStore,
    display: Callable[[str], None],
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Credentials:
    origin = https_origin(scheduler)
    with httpx.Client(timeout=30, follow_redirects=False, transport=transport) as client:
        challenge: dict[str, Any] = checked(client.post(origin + "/api/auth/terminal"))
        verification_uri = str(challenge["verification_uri"])
        if urlsplit(verification_uri).scheme != "https" or (
            urlsplit(verification_uri).netloc != urlsplit(origin).netloc
        ):
            raise PlatformError("LOGIN_PROTOCOL", "Scheduler returned a different login origin")
        display("Open " + verification_uri + " in a browser and sign in with Google.")
        display("Enter terminal code: " + str(challenge["user_code"]))
        expires = min(600, max(1, int(challenge["expires_in"])))
        deadline = time.monotonic() + expires
        interval = max(5, min(30, int(challenge["interval"])))
        while time.monotonic() < deadline:
            sleep(interval)
            try:
                body = checked(
                    client.post(
                        origin + "/api/auth/terminal/poll",
                        json={
                            "credential": challenge["device_secret"],
                        },
                    )
                )
            except PlatformError as error:
                if error.code == "AUTHORIZATION_PENDING":
                    continue
                if error.code == "SLOW_DOWN":
                    interval = min(30, interval + 5)
                    continue
                raise
            credentials = Credentials.from_response(origin, body)
            with store.locked():
                store.save(credentials)
            display("Logged in to " + origin)
            return credentials
    raise PlatformError("LOGIN_EXPIRED", "Login approval expired; run fl-client login again")
