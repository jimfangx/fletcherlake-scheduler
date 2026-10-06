"""Owner checks, immutable download requests and deletion fences use real agent metadata."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fl_common.models import JobConfig
from fl_common.models.download import DownloadRequest
from fl_common.models.scheduler import Principal, Role
from fl_common.models.transfer import TransferEndpoint
from fl_common.ssh import create_identity
from fl_scheduler.agents.commands import Commands
from fl_scheduler.api.control import Control
from fl_scheduler.artifacts.api import Downloads
from fl_scheduler.artifacts.models import Download, Export
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.auth.google import Identity
from fl_scheduler.db.models import Artifact, Command
from fl_scheduler.scheduler.placement import Placement
from fl_scheduler.service import create_app
from sqlalchemy import func, select

from tests.connected import state, until

OWNER = Principal(email="alice@example.edu", subject="google-alice", role=Role.USER)


async def completed(db):
    spec = Placement(db).submit(JobConfig(run_timeout=3), OWNER)
    Placement(db).reserve_pending()
    Commands(db).enqueue_after_transfer(spec.job_id)
    await until(lambda: state(db, spec.job_id) == "SUCCEEDED")

    def indexed():
        with db.transaction() as session:
            return session.get(Artifact, (spec.job_id, "results")) is not None

    await until(indexed)
    return spec


async def test_download_owner_nonce_and_deletion_fences_precede_gateway_calls(
    scheduler_db, connected_agents, auth_stack, tmp_path
):
    spec = await completed(scheduler_db)
    endpoint = TransferEndpoint(host="transfer.test", host_key=create_identity(tmp_path / "host"))

    class NoGateway:
        async def register(self, grant):
            raise AssertionError("An unverified export cannot issue a read scope")

    downloads = Downloads(
        SimpleNamespace(state=SimpleNamespace(db=scheduler_db), gateway=NoGateway()), endpoint
    )
    sessions, login, directory, _, _ = auth_stack
    directory.members["users"].add("bob@example.edu")
    alice = await sessions.issue(Identity(OWNER.subject, OWNER.email))
    bob = await sessions.issue(Identity("bob", "bob@example.edu"))
    app = create_app(
        scheduler_db,
        AuthAPI(sessions, login, "https://scheduler.test"),
        maintenance=False,
        downloads=downloads,
    )
    path = f"/api/jobs/{spec.job_id}/downloads"
    body = DownloadRequest(
        request_id=uuid4(), public_key=create_identity(tmp_path / "reader"), kinds=["results"]
    ).model_dump(mode="json")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://scheduler.test"
    ) as client:
        client.headers["Authorization"] = "Bearer " + bob.access_token.get_secret_value()
        assert (await client.post(path, json=body)).status_code == 403
        client.headers["Authorization"] = "Bearer " + alice.access_token.get_secret_value()
        replies = await asyncio.gather(*(client.post(path, json=body) for _ in range(4)))
        assert all(reply.status_code == 200 for reply in replies)
        assert all(reply.json() == replies[0].json() for reply in replies)
        assert replies[0].json()["state"] == "WAITING" and replies[0].json()["grant"] is None
        changed = {**body, "public_key": create_identity(tmp_path / "other-reader")}
        conflict = await client.post(path, json=changed)
        assert conflict.status_code == 409 and conflict.json()["code"] == "DOWNLOAD_ID_CONFLICT"
        assert (
            await client.post(path, json={**body, "filename": "/etc/passwd"})
        ).status_code == 422
        Control(scheduler_db).delete_artifacts(spec.job_id, OWNER)
        deleted = await client.post(path, json=body)
        assert deleted.status_code == 409 and deleted.json()["code"] == "ARTIFACT_EXPIRED"
    with scheduler_db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Export)) == 1
        assert session.scalar(select(func.count()).select_from(Download)) == 1
        stages = [
            command
            for command in session.scalars(select(Command))
            if command.envelope["type"] == "ARTIFACT_PREPARE"
        ]
        assert len(stages) == 1
