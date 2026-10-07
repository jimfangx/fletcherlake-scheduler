"""Authenticated HTTP delivery accepts a verified gateway receipt, never a client assertion."""

import httpx
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.auth.google import Identity
from fl_scheduler.service import create_app

from tests.integration.test_transfer_delivery import OWNER, setup_delivery


async def test_public_delivery_requires_owner_and_replays_same_scope(
    scheduler_db,
    connected_agents,
    rclone_gateway,
    auth_stack,
    tmp_path,
):
    spec, upload, coordinator, gateway_client, _, _ = await setup_delivery(
        scheduler_db, connected_agents, rclone_gateway, tmp_path
    )
    sessions, login, directory, _, _ = auth_stack
    directory.members["users"].update({OWNER.email, "bob@example.edu"})
    alice = await sessions.issue(Identity(OWNER.subject, OWNER.email))
    bob = await sessions.issue(Identity("bob", "bob@example.edu"))
    app = create_app(
        scheduler_db,
        AuthAPI(sessions, login, "https://scheduler.test"),
        maintenance=False,
        transfers=coordinator,
    )
    path = f"/api/jobs/{spec.job_id}/delivery"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://scheduler.test"
        ) as client:
            body = {"upload_id": str(upload.transfer_id)}
            # Cookie-style mutation without a trusted Origin is rejected before
            # session authentication; terminal bearer requests follow below.
            client.headers["Origin"] = "https://scheduler.test"
            assert (await client.post(path, json=body)).status_code == 401
            client.headers["Authorization"] = "Bearer " + bob.access_token.get_secret_value()
            assert (await client.post(path, json=body)).status_code == 403
            client.headers["Authorization"] = "Bearer " + alice.access_token.get_secret_value()
            assert (await client.post(path, json={**body, "verified": True})).status_code == 422
            first = await client.post(path, json=body)
            second = await client.post(path, json=body)
            assert first.status_code == second.status_code == 202
            assert first.json() == second.json()
            assert first.json()["job_id"] == str(spec.job_id)
            assert first.headers["cache-control"] == "no-store"
    finally:
        await gateway_client.aclose()
