"""Setup reports READY only after asynchronous launchd startup actually succeeds."""

from pathlib import Path

import httpx
import pytest
from fl import Cluster
from fl_cli.readiness import wait_ready
from fl_common.errors import PlatformError


def client_sequence(values):
    calls = []

    def handle(request):
        calls.append(request)
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return httpx.Response(200, json={"state": value})

    client = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://agent")
    return client, calls


def test_readiness_retries_transport_failure_and_startup_states():
    client, calls = client_sequence(
        iter([httpx.ConnectError("socket not created"), "CONFIGURING", "READY"])
    )
    sleeps = []
    snapshot = wait_ready(
        Path("unused.sock"), client_factory=lambda _: Cluster(client), sleep=sleeps.append
    )
    assert snapshot["state"] == "READY" and len(calls) == 3 and len(sleeps) == 2
    assert client.is_closed and client.timeout.read == 1


def test_readiness_fails_promptly_on_invalid_confirmation():
    client, calls = client_sequence(iter(["CONFIGURATION_INCOMPLETE"]))
    with pytest.raises(PlatformError) as error:
        wait_ready(Path("unused.sock"), client_factory=lambda _: Cluster(client))
    assert error.value.code == "CONFIGURATION_INCOMPLETE"
    assert client.is_closed and len(calls) == 1


def test_readiness_deadline_always_closes_client():
    client, calls = client_sequence(iter([]))
    with pytest.raises(PlatformError) as error:
        wait_ready(Path("unused.sock"), timeout=0, client_factory=lambda _: Cluster(client))
    assert error.value.code == "AGENT_NOT_READY"
    assert client.is_closed and not calls
