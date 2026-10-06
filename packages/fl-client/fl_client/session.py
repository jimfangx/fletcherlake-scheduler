"""Start terminal approval when submission has no usable cached human session."""

from collections.abc import Callable

from fl_common.errors import PlatformError
from fl_common.network import https_origin

from .api import RemoteClient
from .auth import login


def ensure_login(client: RemoteClient, origin: str | None, display: Callable[[str], None]) -> None:
    requested = https_origin(origin) if origin is not None else None
    try:
        current = client.origin()
        if requested is not None and requested != current:
            raise PlatformError(
                "PROFILE_ORIGIN", "Use --credentials for a separate scheduler profile"
            )
        client.request("GET", "/api/auth/me")
    except PlatformError as error:
        if error.code not in {"LOGIN_REQUIRED", "UNAUTHENTICATED"}:
            raise
        # An expired profile still identifies the destination. Read it under the
        # same protection/rotation lock; an absent profile needs an explicit origin.
        with client.store.locked():
            try:
                current = client.store.load().scheduler
            except FileNotFoundError:
                current = None
        if requested is not None and current is not None and requested != current:
            raise PlatformError(
                "PROFILE_ORIGIN", "Use --credentials for a separate scheduler profile"
            ) from None
        destination = requested or current
        if destination is None:
            raise PlatformError(
                "LOGIN_REQUIRED", "Supply --scheduler HTTPS_ORIGIN for initial terminal approval"
            ) from None
        login(destination, client.store, display)
        return
