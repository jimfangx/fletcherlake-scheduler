"""Export retry state survives worker replacement; deletion never affects completed hardware."""

import asyncio
from datetime import timedelta

import pytest
from fl_common.bbcp import BBCP
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_scheduler.api.control import Control
from fl_scheduler.artifacts.models import Export
from fl_scheduler.artifacts.service import ExportService
from fl_scheduler.db.models import Command
from sqlalchemy import select

from tests.connected import state, until
from tests.integration.test_artifact_downloads import OWNER


async def test_export_retry_after_worker_replacement_reuses_scope_and_verifies_bytes(export_stack):
    stack = export_stack
    real = BBCP(str(stack.binary))

    class FailOnce:
        calls = 0

        async def copy(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise PlatformError("TRANSFER_FAILED", "Injected first upload disconnect")
            await real.copy(*args)

    transport = FailOnce()
    stack.target.exports.transport = transport
    await stack.service.advance(stack.export_id)
    await until(lambda: stack.service.state.work(stack.export_id).publish is not None)
    first = stack.service.state.work(stack.export_id).publish
    assert not first.accepted and first.error_code == "TRANSFER_FAILED"
    with stack.db.transaction() as session:
        session.get(Export, stack.export_id).next_attempt_at = utcnow() - timedelta(seconds=1)
    replacement = ExportService(
        stack.db, stack.service.gateway, stack.endpoint, simulation_networks=("127.0.0.1/32",)
    )
    await replacement.advance(stack.export_id)

    def accepted():
        ack = replacement.state.work(stack.export_id).publish
        return ack is not None and ack.accepted

    await until(accepted)
    await replacement.advance(stack.export_id)
    # Gateway sealing before READY may be replayed after a lost DB commit.
    with stack.db.transaction() as session:
        session.get(Export, stack.export_id).state = "UPLOADING"
    await replacement.advance(stack.export_id)
    assert stack.store.lookup(stack.export_id, active=False)[1] == "VERIFIED"
    assert transport.calls == 2
    ready = await stack.downloads.request(stack.spec.job_id, stack.body, OWNER)
    assert ready.state == "READY" and ready.grant.public_download
    path = stack.identity.with_name("downloaded-results")
    await real.copy(path, ready.endpoint, ready.grant, "results", stack.identity)
    assert path.read_bytes() == stack.target.store.path(stack.spec.job_id, "results").read_bytes()
    with stack.db.transaction() as session:
        commands = [
            command
            for command in session.scalars(select(Command))
            if command.envelope["type"] == "ARTIFACT_PUBLISH"
        ]
    assert len(commands) == 2 and commands[0].message_id != commands[1].message_id
    assert commands[0].envelope["payload"] == commands[1].envelope["payload"]
    assert state(stack.db, stack.spec.job_id) == "SUCCEEDED"
    Control(stack.db).delete_artifacts(stack.spec.job_id, OWNER)
    await replacement.advance(stack.export_id)
    assert stack.store.lookup(stack.export_id, active=False)[1] == "REVOKED"
    assert stack.store.lookup(ready.download_id, active=False)[1] == "REVOKED"
    with pytest.raises(PlatformError):
        await stack.downloads.request(stack.spec.job_id, stack.body, OWNER)


async def test_delete_during_blocked_publication_revokes_scope_and_cannot_issue_reads(export_stack):
    stack = export_stack
    entered, stopped = asyncio.Event(), asyncio.Event()

    class Blocked:
        async def copy(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    stack.target.exports.transport = Blocked()
    await stack.service.advance(stack.export_id)
    async with asyncio.timeout(5):
        await entered.wait()
    assert stack.target.scheduler_link.connected
    Control(stack.db).delete_artifacts(stack.spec.job_id, OWNER)
    await stack.service.advance(stack.export_id)
    assert stack.store.lookup(stack.export_id, active=False)[1] == "REVOKED"
    with pytest.raises(PlatformError) as error:
        await stack.downloads.request(stack.spec.job_id, stack.body, OWNER)
    assert error.value.code == "ARTIFACT_EXPIRED"
    async with asyncio.timeout(5):
        await stopped.wait()
    await until(lambda: stack.service.state.work(stack.export_id).publish is not None)
    assert stack.target.scheduler_link.connected
    assert state(stack.db, stack.spec.job_id) == "SUCCEEDED"


async def test_local_delete_reaps_publication_before_removing_source_files(export_stack):
    import httpx
    from fl_agent.api import create_app

    stack = export_stack
    entered = asyncio.Event()
    source = stack.target.store.path(stack.spec.job_id, "results")
    stopped = []

    class Blocked:
        async def copy(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                # The deletion fence is durable, but physical files remain until this exits.
                assert source.is_file()
                assert stack.target.db.connection.execute(
                    "SELECT 1 FROM collateral_deletion WHERE job_id=?", (str(stack.spec.job_id),)
                ).fetchone()
                stopped.append(True)

    stack.target.exports.transport = Blocked()
    await stack.service.advance(stack.export_id)
    async with asyncio.timeout(5):
        await entered.wait()
    async with httpx.AsyncClient(
        base_url="http://fl-agent", transport=httpx.ASGITransport(app=create_app(stack.target))
    ) as local:
        response = await local.delete(f"/v1/jobs/{stack.spec.job_id}/artifacts")
    assert response.status_code == 204 and stopped == [True]
    assert not source.exists() and not stack.target.exports.tasks
    assert stack.target.db.get(stack.spec.job_id).state == "SUCCEEDED"
    assert stack.target.scheduler_link.connected
