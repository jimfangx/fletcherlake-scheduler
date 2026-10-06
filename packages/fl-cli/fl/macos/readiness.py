"""Wait for launchd's asynchronous startup before claiming that setup is READY."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fl_common.errors import PlatformError

from ..sdk import Cluster


def wait_ready(
    socket: Path,
    *,
    timeout: float = 90,
    client_factory: Callable[[Path], Cluster] = Cluster.local,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    client = client_factory(socket)
    client.client.timeout = httpx.Timeout(1)
    try:
        while time.monotonic() < deadline:
            try:
                snapshot = client.status()
            except httpx.TransportError:
                sleep(0.25)
                continue
            if snapshot["state"] == "READY":
                return snapshot
            if snapshot["state"] == "CONFIGURATION_INCOMPLETE":
                raise PlatformError(
                    "CONFIGURATION_INCOMPLETE", "Agent rejected the confirmation marker"
                )
            sleep(0.25)
        raise PlatformError(
            "AGENT_NOT_READY", "Agent startup did not become READY before the deadline"
        )
    finally:
        client.close()
