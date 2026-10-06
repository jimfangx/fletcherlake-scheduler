"""Submission approval is automatic, bounded, and tied to the selected profile."""

from datetime import timedelta

import httpx
import pytest
from fl_client.api import RemoteClient
from fl_client.credentials import Credentials, CredentialStore
from fl_client.receipts import ReceiptStore
from fl_client.session import ensure_login
from fl_client.submission import Submission
from fl_common.errors import PlatformError
from fl_common.models import JobConfig
from fl_common.models.base import utcnow
from fl_common.models.scheduler import Principal, Role
from pydantic import SecretStr


def save(store, *, expired=False):
    with store.locked():
        store.save(
            Credentials(
                scheduler="https://scheduler.test",
                access_token=SecretStr("human-access"),
                refresh_token=SecretStr("human-refresh"),
                access_expires_at=utcnow() + timedelta(minutes=-15 if expired else 15),
                refresh_expires_at=utcnow() + timedelta(days=-1 if expired else 1),
            )
        )


@pytest.mark.parametrize("expired", [False, True])
def test_missing_or_expired_profile_automatically_starts_approval(tmp_path, monkeypatch, expired):
    store = CredentialStore(tmp_path / "profile" / "credentials.json")
    if expired:
        save(store, expired=True)
    approvals = []

    def approve(origin, selected, display):
        approvals.append(origin)
        assert selected is store
        save(store)

    monkeypatch.setattr("fl_client.session.login", approve)
    with RemoteClient(store) as client:
        ensure_login(client, None if expired else "https://scheduler.test", lambda _: None)
        assert client.origin() == "https://scheduler.test"
    assert approvals == ["https://scheduler.test"]


def test_cached_session_checks_identity_and_rejects_a_different_scheduler(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / "profile" / "credentials.json")
    save(store)
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == "scheduler.test" and request.url.path == "/api/auth/me"
        return httpx.Response(200, json={"email": "alice", "subject": "alice", "role": "user"})

    monkeypatch.setattr("fl_client.session.login", lambda *_: pytest.fail("Unexpected approval"))
    with RemoteClient(store, transport=httpx.MockTransport(handle)) as client:
        ensure_login(client, None, lambda _: None)
        with pytest.raises(PlatformError) as error:
            ensure_login(client, "https://other.test", lambda _: None)
        assert error.value.code == "PROFILE_ORIGIN"
    assert len(calls) == 1


def test_provider_outage_does_not_start_repeated_approval(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / "profile" / "credentials.json")
    save(store)
    with store.locked():
        credentials = store.load()
        credentials.access_expires_at = utcnow() - timedelta(seconds=1)
        store.save(credentials)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503, json={"code": "AUTH_UNAVAILABLE", "message": "Directory outage"})

    monkeypatch.setattr("fl_client.session.login", lambda *_: pytest.fail("Unexpected approval"))
    with RemoteClient(store, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(PlatformError) as error:
            ensure_login(client, None, lambda _: None)
        assert error.value.code == "AUTH_UNAVAILABLE"
    assert len(calls) == 1


def test_lost_submission_response_receipt_cannot_create_a_job_under_another_login(tmp_path):
    store = CredentialStore(tmp_path / "profile" / "credentials.json")
    save(store)
    receipts = ReceiptStore(tmp_path / "receipt" / "request.json")
    with receipts.lock():
        receipt = receipts.prepare("https://scheduler.test", JobConfig())
        receipt.principal = Principal(email="alice", subject="alice", role=Role.USER)
        receipts.save(receipt)
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/api/auth/me"
        return httpx.Response(200, json={"email": "bob", "subject": "bob", "role": "user"})

    with RemoteClient(store, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(PlatformError) as error:
            Submission(client, display=lambda _: None).run(receipts)
        assert error.value.code == "SUBMISSION_OWNER"
    assert len(calls) == 1
