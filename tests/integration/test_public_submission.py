"""Ordinary-user grants require a live reservation and an immutable request identity."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fl_client.receipts import ReceiptStore
from fl_common.errors import PlatformError
from fl_common.models import JobConfig, ResourceConstraints
from fl_common.models.base import utcnow
from fl_common.models.scheduler import Principal, Role
from fl_common.models.transfer import TransferEndpoint, TransferGrant
from fl_common.ssh import create_identity
from fl_scheduler.api.control import Control
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.auth.google import Identity
from fl_scheduler.db.models import Assignment, Job
from fl_scheduler.service import Maintenance, create_app
from fl_scheduler.transfers.models import Upload
from fl_scheduler.transfers.submissions import Submissions
from fl_scheduler.transfers.upload_cleanup import UploadCleanup
from sqlalchemy import func, select

from tests.connected import state, until

OWNER = Principal(email="alice@example.edu", subject="google-alice", role=Role.USER)


class Gateway:
    def __init__(self):
        self.grants = {}
        self.revoked = []
        self.unavailable = False
        self.on_register = lambda: None

    async def register(self, grant):
        self.grants[grant.transfer_id] = grant
        self.on_register()

    async def revoke(self, transfer_id):
        if self.unavailable:
            raise PlatformError("TRANSFER_GATEWAY_UNAVAILABLE", "Injected outage")
        self.revoked.append(transfer_id)


def submissions(db, tmp_path):
    gateway = Gateway()
    endpoint = TransferEndpoint(
        host="transfer.test", username="collateral", host_key=create_identity(tmp_path / "host")
    )
    return Submissions(
        db, SimpleNamespace(gateway=gateway), endpoint, "https://transfer.test"
    ), gateway


def request(tmp_path, board=None):
    source = tmp_path / "input.elf"
    source.write_bytes(b"request input")
    store = ReceiptStore(tmp_path / "receipt" / "request.json")
    with store.lock():
        return store.prepare(
            "https://scheduler.test",
            JobConfig(binary=str(source), resource_constraints=ResourceConstraints(board=board)),
        ).request


async def test_public_request_nonce_replay_conflict_and_owner(scheduler_db, auth_stack, tmp_path):
    service, gateway = submissions(scheduler_db, tmp_path)
    sessions, login, directory, _, _ = auth_stack
    directory.members["users"].add("bob@example.edu")
    alice = await sessions.issue(Identity(OWNER.subject, OWNER.email))
    bob = await sessions.issue(Identity("google-bob", "bob@example.edu"))
    app = create_app(
        scheduler_db,
        AuthAPI(sessions, login, "https://scheduler.test"),
        maintenance=False,
        submissions=service,
    )
    body = request(tmp_path).model_dump(mode="json")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://scheduler.test"
    ) as client:
        client.headers["Origin"] = "https://scheduler.test"
        assert (await client.post("/api/submissions", json=body)).status_code == 401
        client.headers["Authorization"] = "Bearer " + alice.access_token.get_secret_value()
        replies = await asyncio.gather(
            *(client.post("/api/submissions", json=body) for _ in range(4))
        )
        assert all(reply.status_code == 201 for reply in replies)
        assert all(reply.json() == replies[0].json() for reply in replies)
        job_id = replies[0].json()["spec"]["job_id"]
        changed = {**body, "config": {**body["config"], "priority": 1}}
        assert (await client.post("/api/submissions", json=changed)).json()[
            "code"
        ] == "SUBMISSION_ID_CONFLICT"
        assert (
            await client.post("/api/submissions", json={**body, "owner": "bob@example.edu"})
        ).status_code == 422
        ticket = await client.post(f"/api/jobs/{job_id}/upload")
        assert ticket.json()["state"] == "WAITING"
        assert ticket.json()["grant"] is None and not gateway.grants
        wrong_kind = {**body, "binary": {**body["binary"], "kind": "bitstream"}}
        assert (await client.post("/api/submissions", json=wrong_kind)).status_code == 422
        assert (
            await client.post("/api/submissions", json={**body, "binary": None})
        ).status_code == 422
        client.headers["Authorization"] = "Bearer " + bob.access_token.get_secret_value()
        assert (await client.post(f"/api/jobs/{job_id}/upload")).status_code == 403
        # A request nonce is scoped to its owner. A reused transfer key still cannot
        # cross jobs, even when the nonce happens to be the same.
        other = await client.post("/api/submissions", json=body)
        assert other.json()["code"] == "TRANSFER_IDENTITY"
    with scheduler_db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1


async def test_reserved_grant_retry_expiry_and_durable_cancel_cleanup(
    scheduler_db, connected_agents, tmp_path
):
    service, gateway = submissions(scheduler_db, tmp_path)
    board = next(iter(connected_agents[1][0].workers))
    body = request(tmp_path, board)
    job_id = (await service.submit(body, OWNER)).spec.job_id
    service.placement.reserve_pending()
    first = await service.ticket(job_id, OWNER)
    assert first.state == "UPLOAD" and first.grant
    second = await service.ticket(job_id, OWNER)
    assert second.grant.transfer_id == first.grant.transfer_id
    assert second.grant.retains_until == first.grant.retains_until
    assert first.grant.public_key == body.public_key and first.grant.token_hash == body.token_hash
    with scheduler_db.transaction() as session:
        session.get(Assignment, job_id).expires_at = utcnow() - timedelta(seconds=1)
    assert (await service.ticket(job_id, OWNER)).state == "WAITING"
    Control(scheduler_db).cancel(job_id, OWNER)
    with pytest.raises(PlatformError, match="Canceled jobs"):
        await service.ticket(job_id, OWNER)
    gateway.unavailable = True
    await UploadCleanup(scheduler_db, gateway).tick()
    with scheduler_db.transaction() as session:
        upload = session.get(Upload, job_id)
        assert not upload.closed
        upload.next_cleanup_at = utcnow() - timedelta(seconds=1)
    gateway.unavailable = False
    await UploadCleanup(scheduler_db, gateway).tick()
    assert gateway.revoked == [first.grant.transfer_id]
    # Until issued credentials expire, a replacement worker keeps retrying even
    # after successful revocation, guarding a concurrently finishing registration.
    with scheduler_db.transaction() as session:
        upload = session.get(Upload, job_id)
        assert not upload.closed
        grant = TransferGrant.model_validate(upload.grant)
        grant.expires_at = utcnow() - timedelta(seconds=1)
        upload.grant = grant.model_dump(mode="json")
        upload.next_cleanup_at = utcnow() - timedelta(seconds=1)
    await UploadCleanup(scheduler_db, gateway).tick()
    with scheduler_db.transaction() as session:
        assert session.get(Upload, job_id).closed


async def test_cancel_during_gateway_registration_is_rechecked(
    scheduler_db, connected_agents, tmp_path
):
    service, gateway = submissions(scheduler_db, tmp_path)
    board = next(iter(connected_agents[1][0].workers))
    job_id = (await service.submit(request(tmp_path, board), OWNER)).spec.job_id
    service.placement.reserve_pending()
    gateway.on_register = lambda: Control(scheduler_db).cancel(job_id, OWNER)
    with pytest.raises(PlatformError, match="closed during"):
        await service.ticket(job_id, OWNER)
    assert gateway.revoked == list(gateway.grants)
    assert state(scheduler_db, job_id) == "CANCELED"


async def test_inputless_submission_is_automatically_enqueued(
    scheduler_db, connected_agents, auth_stack, tmp_path
):
    from fl_common.models.submission import SubmissionRequest

    service, _ = submissions(scheduler_db, tmp_path)
    body = SubmissionRequest(request_id=uuid4(), config=JobConfig(run_timeout=2))
    spec = (await service.submit(body, OWNER)).spec
    sessions, login, _, _, _ = auth_stack
    maintenance = Maintenance(scheduler_db, AuthAPI(sessions, login, "https://scheduler.test"))
    maintenance.tick()
    maintenance.tick()
    await until(lambda: state(scheduler_db, spec.job_id) == "SUCCEEDED")
    assert (await service.ticket(spec.job_id, OWNER)).state == "COMPLETE"
