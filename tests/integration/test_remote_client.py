"""Terminal login/control over the public API, with no root/private-network operations."""

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from fl_client.api import RemoteClient
from fl_client.auth import login
from fl_client.credentials import CredentialStore
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_scheduler.auth.models import HumanSession
from fl_scheduler.auth.sessions import digest
from sqlalchemy import select


def bridge(browser):
    def handle(request):
        response = browser.request(
            request.method,
            request.url.path,
            params=request.url.params,
            headers=request.headers,
            content=request.content,
        )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    return httpx.MockTransport(handle)


def test_terminal_login_status_cancel_rotation_and_logout(auth_stack, scheduler_db, tmp_path):
    _, _, _, _, app = auth_stack
    store = CredentialStore(tmp_path / "private" / "client.json")
    displayed = []
    with TestClient(app, base_url="https://scheduler.test") as browser:
        transport = bridge(browser)
        sleeps = []

        def approve_after_first_poll(duration):
            sleeps.append(duration)
            if len(sleeps) == 2:
                response = browser.get("/api/auth/login", follow_redirects=False)
                state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
                response = browser.get(
                    "/api/auth/callback",
                    params={"state": state, "code": "google-code"},
                    follow_redirects=False,
                )
                assert response.status_code == 303
                code = displayed[1].removeprefix("Enter terminal code: ")
                response = browser.post(
                    "/api/auth/terminal/approve",
                    json={"user_code": code},
                    headers={"Origin": "https://scheduler.test"},
                )
                assert response.status_code == 204
                # Advance only the broker polling rate limit; no real five-second test delay.
                from fl_scheduler.auth.models import DeviceLogin

                with scheduler_db.transaction() as session:
                    session.scalar(select(DeviceLogin)).last_poll_at = utcnow() - timedelta(
                        seconds=6
                    )

        tokens = login(
            "https://scheduler.test",
            store,
            displayed.append,
            transport=transport,
            sleep=approve_after_first_poll,
        )
        assert sleeps == [5, 5]
        assert all(tokens.access_token.get_secret_value() not in line for line in displayed)
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.path.parent.stat().st_mode & 0o777 == 0o700
        with RemoteClient(store, transport=transport) as client:
            spec = client.request("POST", "/api/jobs", json={"config": {}})
            job_id = UUID(spec["job_id"])
            assert len(client.jobs()) == 1
            assert client.status(job_id)["state"] == "CREATED"
            client.cancel(job_id)
            assert client.status(job_id)["state"] == "CANCELED"
            with scheduler_db.transaction() as session:
                row = session.scalar(
                    select(HumanSession).where(
                        HumanSession.access_hash == digest(tokens.access_token.get_secret_value())
                    )
                )
                row.access_expires_at = utcnow() - timedelta(seconds=1)
            # A 401 refreshes once and atomically replaces the stored credentials.
            assert client.status(job_id)["state"] == "CANCELED"
            with store.locked():
                assert store.load().access_token != tokens.access_token
            client.logout()
        assert not store.path.exists()
        response = browser.get(
            "/api/jobs",
            headers={"Authorization": "Bearer " + tokens.access_token.get_secret_value()},
        )
        assert response.status_code == 401


def test_remote_client_never_sends_tokens_to_redirect_target(auth_stack, tmp_path):
    from fl_client.credentials import Credentials
    from pydantic import SecretStr

    store = CredentialStore(tmp_path / "private" / "client.json")
    credentials = Credentials(
        scheduler="https://scheduler.test",
        access_token=SecretStr("secret-access"),
        refresh_token=SecretStr("secret-refresh"),
        access_expires_at=utcnow() + timedelta(minutes=15),
        refresh_expires_at=utcnow() + timedelta(days=7),
    )
    with store.locked():
        store.save(credentials)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://evil.test/steal"})

    with RemoteClient(store, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(PlatformError, match="redirects are not accepted"):
            client.jobs()
    assert len(calls) == 1 and calls[0].url.host == "scheduler.test"
