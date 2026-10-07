"""Client SHA verification, protected identity and no-clobber publication bound public reads."""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fl_client.api import RemoteClient
from fl_client.credentials import Credentials, CredentialStore
from fl_client.download_receipts import DownloadReceipts
from fl_client.results import Results
from fl_common.errors import PlatformError
from fl_common.models import ArtifactRecord, ArtifactRef
from fl_common.models.base import utcnow
from fl_common.models.download import DownloadTicket
from fl_common.models.transfer import TransferEndpoint, TransferGrant
from fl_common.ssh import create_identity
from pydantic import SecretStr


@pytest.mark.parametrize("mode", ["success_resume", "lost_response", "corrupt", "existing_file"])
def test_public_download_verifies_bytes_and_never_clobbers_different_files(tmp_path, mode):
    job_id, download_id, source_id = uuid4(), uuid4(), uuid4()
    payload = b'{"passed":true}'
    artifact = ArtifactRecord(
        job_id=job_id,
        expires_at=utcnow() + timedelta(days=1),
        ref=ArtifactRef(
            kind="results", sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload)
        ),
    )
    endpoint = TransferEndpoint(host="transfer.test", host_key=create_identity(tmp_path / "host"))
    credentials = CredentialStore(tmp_path / "profile" / "credentials.json")
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
    destination = tmp_path / "results"
    destination.mkdir()
    if mode == "existing_file":
        (destination / "results.json").write_bytes(b"local user file")
    requests = []

    def handle(request):
        assert request.url.host == "scheduler.test"
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json={"email": "alice", "subject": "alice", "role": "user"})
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(200, json=[artifact.model_dump(mode="json")])
        body = json.loads(request.content)
        requests.append(body)
        grant = TransferGrant(
            job_id=job_id,
            transfer_id=download_id,
            source_id=source_id,
            direction="download",
            public_download=True,
            public_key=body["public_key"],
            token_hash="0" * 64,
            files=[artifact.ref],
            expires_at=utcnow() + timedelta(minutes=10),
            retains_until=artifact.expires_at,
        )
        ticket = DownloadTicket(
            job_id=job_id,
            download_id=download_id,
            state="READY",
            artifacts=[artifact],
            grant=grant,
            endpoint=endpoint,
        )
        if mode == "lost_response" and len(requests) == 1:
            raise httpx.ReadError("Injected lost download response")
        return httpx.Response(200, json=ticket.model_dump(mode="json"))

    class Transport:
        calls = 0

        async def copy(self, local, endpoint, grant, kind, identity):
            self.calls += 1
            local.write_bytes(b"corrupted" if mode == "corrupt" else payload)

    transport = Transport()
    with RemoteClient(credentials, transport=httpx.MockTransport(handle)) as client:
        workflow = Results(client, rclone=transport, display=lambda _: None)
        if mode in {"success_resume", "lost_response"}:
            if mode == "lost_response":
                with pytest.raises(httpx.ReadError):
                    workflow.run(job_id, destination)
            paths = workflow.run(job_id, destination)
            assert workflow.run(job_id, destination) == paths
            assert paths[0].read_bytes() == payload and transport.calls == 1
            assert requests[0] == requests[1]
            assert paths[0].stat().st_mode & 0o777 == 0o600
        else:
            with pytest.raises(PlatformError) as error:
                workflow.run(job_id, destination)
            assert error.value.code == (
                "OUTPUT_EXISTS" if mode == "existing_file" else "ARTIFACT_INTEGRITY"
            )
            if mode == "existing_file":
                assert transport.calls == 0
                assert (destination / "results.json").read_bytes() == b"local user file"
            else:
                assert not (destination / "results.json").exists()
    receipts = DownloadReceipts(destination)
    assert receipts.path.stat().st_mode & 0o777 == 0o600
    assert receipts.identity.stat().st_mode & 0o777 == 0o600
    assert receipts.root.stat().st_mode & 0o777 == 0o700
    assert not list(destination.glob(".download-*"))
