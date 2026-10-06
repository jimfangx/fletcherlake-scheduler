"""Live binary logs over real agent WebSockets, with HTTP ownership and retention fences."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from fl_common.models import JobConfig, ResourceConstraints
from fl_common.models.base import utcnow
from fl_common.models.logs import LogPage
from fl_scheduler.agents.commands import Commands
from fl_scheduler.auth.google import Identity
from fl_scheduler.db.models import Command, Job
from fl_scheduler.scheduler.placement import Placement
from sqlalchemy import select

from tests.connected import GatewayServer, state, until
from tests.integration.test_artifact_downloads import OWNER, completed


async def ready(client, path, offset=0, limit=65536):
    async with asyncio.timeout(15):
        while True:
            response = await client.get(path, params={"offset": offset, "limit": limit})
            assert response.status_code == 200, response.text
            page = LogPage.model_validate(response.json())
            if page.state == "READY":
                return page
            await asyncio.sleep(0.05)


async def test_binary_uart_is_readable_before_completion_and_resumes_after_disconnect(
    scheduler_db, connected_agents, auth_stack
):
    server, services = connected_agents
    target = services[0]
    worker = next(iter(target.workers.values()))
    payload = bytes(range(256)) * 520 + "UART: 🧪\n".encode()
    worker.backend.behavior.uart = payload
    worker.backend.behavior.run_seconds = 10
    spec = Placement(scheduler_db).submit(
        JobConfig(resource_constraints=ResourceConstraints(board=worker.board_id), run_timeout=15),
        OWNER,
    )
    Placement(scheduler_db).reserve_pending()
    Commands(scheduler_db).enqueue_after_transfer(spec.job_id)
    await until(lambda: state(scheduler_db, spec.job_id) == "RUNNING")
    await until(lambda: target.logs.head(spec.job_id, "stdout").size_bytes == len(payload))
    sessions, _, _, provider, app = auth_stack
    tokens = await sessions.issue(provider.identity)
    path = f"/api/jobs/{spec.job_id}/logs"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://scheduler.test",
        headers={"Authorization": "Bearer " + tokens.access_token.get_secret_value()},
    ) as client:
        await asyncio.gather(*(client.get(path) for _ in range(4)))
        first = await ready(client, path)
        assert first.chunk.data() == payload[:65536] and not first.chunk.eof
        assert state(scheduler_db, spec.job_id) == "RUNNING"
        with scheduler_db.transaction() as session:
            reads = list(
                session.scalars(
                    select(Command).where(Command.envelope["type"].as_string() == "LOG_READ")
                )
            )
            assert len(reads) == 1
        await server.stop()
        assert (await ready(client, path)).chunk == first.chunk
        waiting = await client.get(path, params={"offset": first.chunk.next_offset})
        assert waiting.json()["state"] == "WAITING" and worker.backend.running
        replacement = GatewayServer(scheduler_db, port=server.port)
        await replacement.start()
        try:
            chunks, offset = [first.chunk.data()], first.chunk.next_offset
            while offset < len(payload):
                page = await ready(client, path, offset)
                chunks.append(page.chunk.data())
                offset = page.chunk.next_offset
            assert b"".join(chunks) == payload
            await until(lambda: state(scheduler_db, spec.job_id) == "SUCCEEDED")
            end = await ready(client, path, offset)
            assert end.chunk.data() == b"" and end.chunk.eof
            stderr = await client.get(path, params={"stream": "stderr"})
            assert stderr.json()["chunk"]["eof"] is True
            events = target.db.job_events(spec.job_id)
            assert [event.type for event in events].count("JOB_RUNNING") == 1
            assert "JOB_INTERRUPTED" not in [event.type for event in events]
            log_events = [event for event in events if event.type == "JOB_LOG"]
            assert log_events[0].payload == {
                "stream": "stdout",
                "offset": 0,
                "next_offset": len(payload),
            }
        finally:
            await replacement.stop()


@pytest.mark.parametrize("reason", ["delete", "expire"])
async def test_owner_and_retention_checks_precede_cached_log_bytes(
    scheduler_db, connected_agents, auth_stack, reason
):
    server, _ = connected_agents
    spec = await completed(scheduler_db)
    sessions, _, directory, provider, app = auth_stack
    directory.members["users"].add("bob@example.edu")
    alice = await sessions.issue(provider.identity)
    bob = await sessions.issue(Identity("google-bob", "bob@example.edu"))
    path = f"/api/jobs/{spec.job_id}/logs"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://scheduler.test"
    ) as client:
        client.headers["Authorization"] = "Bearer " + bob.access_token.get_secret_value()
        assert (await client.get(path)).status_code == 403
        with scheduler_db.transaction() as session:
            assert not list(
                session.scalars(
                    select(Command).where(Command.envelope["type"].as_string() == "LOG_READ")
                )
            )
        client.headers["Authorization"] = "Bearer " + alice.access_token.get_secret_value()
        for params in ({"limit": 65537}, {"offset": -1}, {"stream": "../../etc/passwd"}):
            assert (await client.get(path, params=params)).status_code == 422
        assert (await ready(client, path)).chunk.data() == b"mock: hello from SoC\n"
        assert (await client.get(path, params={"offset": 999})).json()["code"] == "LOG_OFFSET"
        await server.stop()
        if reason == "delete":
            assert (await client.delete(f"/api/jobs/{spec.job_id}/artifacts")).status_code == 202
        else:
            with scheduler_db.transaction() as session:
                job = session.get(Job, spec.job_id)
                job.record = {
                    **job.record,
                    "finished_at": (
                        utcnow() - timedelta(days=spec.collateral_ttl_days + 1)
                    ).isoformat(),
                }
        denied = await client.get(path)
        assert denied.status_code == 409 and denied.json()["code"] == "ARTIFACT_EXPIRED"
        assert "data_b64" not in denied.text


def test_offline_read_limits_deadlines_and_cleanup_preserve_job_state(scheduler_db, config):
    from fl_common.models.logs import LogHead
    from fl_scheduler.db.models import Cluster
    from fl_scheduler.logs.cleanup import sweep
    from fl_scheduler.logs.reads import LogReads

    from tests.integration.test_public_api import registered_ready

    inventory, owner = registered_ready(scheduler_db, config)
    spec = Placement(scheduler_db).submit(JobConfig(), owner)
    Placement(scheduler_db).reserve_pending()
    initial_state = state(scheduler_db, spec.job_id)
    head = LogHead(
        job_id=spec.job_id,
        stream="stdout",
        size_bytes=100,
        file_id="1",
        terminal=False,
        retained=True,
    )
    with scheduler_db.transaction() as session:
        session.get(Cluster, inventory.cluster_id).snapshot = {
            "logs": [head.model_dump(mode="json")]
        }
    reads = LogReads(scheduler_db)
    for offset in range(16):
        assert reads.page(spec.job_id, owner, "stdout", offset, 1).state == "WAITING"
    from fl_common.errors import PlatformError

    with pytest.raises(PlatformError) as error:
        reads.page(spec.job_id, owner, "stdout", 16, 1)
    assert error.value.code == "SLOW_DOWN"
    with scheduler_db.transaction() as session:
        for row in session.scalars(select(Command)):
            row.created_at = utcnow() - timedelta(seconds=40)
            payload = {
                **row.envelope["payload"],
                "expires_at": (utcnow() - timedelta(seconds=10)).isoformat(),
            }
            row.envelope = {**row.envelope, "payload": payload}
    sweep(scheduler_db)
    with scheduler_db.transaction() as session:
        for row in session.scalars(select(Command)):
            assert row.response["error_code"] == "LOG_REQUEST_EXPIRED"
            row.created_at = utcnow() - timedelta(seconds=70)
    sweep(scheduler_db)
    with scheduler_db.transaction() as session:
        assert not list(session.scalars(select(Command)))
        assert session.get(Job, spec.job_id).state == initial_state
    assert reads.page(spec.job_id, owner, "stdout", 0).state == "WAITING"
