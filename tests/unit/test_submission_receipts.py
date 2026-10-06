"""Durable retry proof rejects changed identities, public files and changed inputs."""

from datetime import timedelta

import httpx
import pytest
from fl_client.api import RemoteClient
from fl_client.credentials import Credentials, CredentialStore
from fl_client.receipts import ReceiptStore
from fl_client.submission import Submission
from fl_common.errors import PlatformError
from fl_common.models import JobConfig, JobSpec
from fl_common.models.base import utcnow
from fl_common.models.submission import UploadTicket
from fl_common.models.transfer import TransferEndpoint, TransferGrant
from fl_common.ssh import create_identity
from pydantic import SecretStr


def prepare(tmp_path, filename="receipt.json"):
    source = tmp_path / "input.elf"
    source.write_bytes(b"original bytes")
    store = ReceiptStore(tmp_path / "private" / filename)
    with store.lock():
        receipt = store.prepare("https://scheduler.test", JobConfig(binary=str(source)))
    return source, store, receipt


@pytest.mark.parametrize("filename", ["receipt.key", "receipt.lock"])
def test_receipt_storage_cannot_alias_its_identity_or_lock(tmp_path, filename):
    _, store, receipt = prepare(tmp_path, filename)
    with store.lock():
        assert store.load() == receipt
        with pytest.raises(PlatformError, match="Use --resume"):
            store.prepare("https://scheduler.test", receipt.request.config)
    store.path.chmod(0o644)
    with pytest.raises(PermissionError):
        store.load()


def test_replaced_private_identity_is_rejected_before_network_io(tmp_path):
    _, store, _ = prepare(tmp_path)
    store.identity.unlink()
    create_identity(store.identity)
    with pytest.raises(PlatformError, match="no longer matches"):
        store.load()


def test_resume_rejects_changed_source_before_bbcp_or_delivery(tmp_path):
    source, store, receipt = prepare(tmp_path)
    source.write_bytes(b"replacement bytes")
    credentials = CredentialStore(tmp_path / "credentials" / "client.json")
    with credentials.locked():
        credentials.save(
            Credentials(
                scheduler="https://scheduler.test",
                access_token=SecretStr("human-access"),
                refresh_token=SecretStr("human-refresh"),
                access_expires_at=utcnow() + timedelta(minutes=15),
                refresh_expires_at=utcnow() + timedelta(days=1),
            )
        )
    request = receipt.request
    spec = JobSpec.from_config(request.config, "alice", binary=request.binary)
    grant = TransferGrant(
        transfer_id=request.request_id,
        job_id=spec.job_id,
        direction="upload",
        public_key=request.public_key,
        token_hash=request.token_hash,
        files=[request.binary],
        expires_at=utcnow() + timedelta(minutes=10),
        retains_until=utcnow() + timedelta(days=1),
    )
    ticket = UploadTicket(
        job_id=spec.job_id,
        state="UPLOAD",
        job_state="STAGING",
        grant=grant,
        endpoint=TransferEndpoint(
            host="transfer.test", host_key=create_identity(tmp_path / "host")
        ),
        verify_origin="https://transfer.test",
    )
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json={"email": "alice", "subject": "alice", "role": "user"})
        if request.url.path == "/api/submissions":
            return httpx.Response(
                201, json={"spec": spec.model_dump(mode="json"), "state": "STAGING"}
            )
        if request.url.path.endswith("/upload"):
            return httpx.Response(200, json=ticket.model_dump(mode="json"))
        assert request.url.host == "transfer.test" and request.url.path.endswith("/verify")
        return httpx.Response(409, json={"code": "TRANSFER_INCOMPLETE", "message": "Missing bytes"})

    with RemoteClient(credentials, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(PlatformError) as error:
            Submission(client, display=lambda _: None).run(store)
        assert error.value.code == "ARTIFACT_CHANGED"
    assert len(calls) == 4
    assert (
        calls[-1].headers["authorization"] == "Bearer " + receipt.staging_token.get_secret_value()
    )
    assert not any(call.url.path.endswith("/delivery") for call in calls)
