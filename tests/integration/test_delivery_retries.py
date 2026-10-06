"""Transient transfer retries and cancellation traverse the live WebSocket command dispatcher."""

import asyncio

import pytest
from fl_common.bbcp import BBCP
from fl_common.errors import PlatformError
from fl_common.protocol import Ack, Message, MessageType
from fl_scheduler.agents.commands import Commands
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.api.control import Control
from fl_scheduler.db.models import Command
from fl_scheduler.registry import Registry
from sqlalchemy import select

from tests.connected import state, until
from tests.integration.test_transfer_delivery import OWNER, delivery_state, setup_delivery


async def test_transient_fetch_failure_retries_with_new_message_and_same_scope(
    scheduler_db,
    connected_agents,
    bbcp_gateway,
    tmp_path,
):
    spec, upload, coordinator, client, target, _ = await setup_delivery(
        scheduler_db, connected_agents, bbcp_gateway, tmp_path
    )
    real = BBCP(str(bbcp_gateway[2]))

    class FailOnce:
        calls = 0

        async def copy(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise PlatformError("TRANSFER_FAILED", "Injected transient payload connection loss")
            await real.copy(*args)

    transport = FailOnce()
    target.transfers.transport = transport
    operation = None
    try:
        await coordinator.begin(spec.job_id, upload.transfer_id, OWNER)
        operation = asyncio.create_task(coordinator.run())
        await until(lambda: state(scheduler_db, spec.job_id) == "SUCCEEDED", seconds=20)
        assert transport.calls == 2
        with scheduler_db.transaction() as session:
            fetches = [
                command
                for command in session.scalars(
                    select(Command)
                    .where(Command.job_id == spec.job_id)
                    .order_by(Command.created_at)
                )
                if command.envelope["type"] == "JOB_FETCH"
            ]
        assert len(fetches) == 2
        assert fetches[0].message_id != fetches[1].message_id
        assert fetches[0].envelope["payload"]["grant"] == fetches[1].envelope["payload"]["grant"]
        assert not fetches[0].response["accepted"] and fetches[1].response["accepted"]
        assert sum(event.type == "JOB_RUNNING" for event in target.db.job_events(spec.job_id)) == 1
    finally:
        if operation:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await client.aclose()


async def test_websocket_cancel_remains_responsive_during_blocked_payload(
    scheduler_db,
    connected_agents,
    bbcp_gateway,
    tmp_path,
):
    spec, upload, coordinator, client, target, _ = await setup_delivery(
        scheduler_db, connected_agents, bbcp_gateway, tmp_path
    )
    entered, stopped = asyncio.Event(), asyncio.Event()

    class BlockedPayload:
        async def copy(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    target.transfers.transport = BlockedPayload()
    operation = None
    try:
        await coordinator.begin(spec.job_id, upload.transfer_id, OWNER)
        operation = asyncio.create_task(coordinator.run())
        async with asyncio.timeout(10):
            await entered.wait()
        Control(scheduler_db).cancel(spec.job_id, OWNER)
        async with asyncio.timeout(3):
            await stopped.wait()
            await until(lambda: state(scheduler_db, spec.job_id) == "CANCELED")
        await until(lambda: delivery_state(scheduler_db, spec.job_id) == "DONE", seconds=10)
        assert not any(event.type == "JOB_RUNNING" for event in target.db.job_events(spec.job_id))
        assert target.scheduler_link.connected
    finally:
        if operation:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await client.aclose()


def test_superseded_agent_session_cannot_acknowledge_delivery(scheduler_db, config):
    cluster = Registry(scheduler_db).register(config, "test-cluster-token")
    reconciler = Reconciler(scheduler_db)
    old = reconciler.connected(cluster.cluster_id)
    current = reconciler.connected(cluster.cluster_id)
    commands = Commands(scheduler_db)
    message = commands.issue(cluster.cluster_id, Message(type=MessageType.STATUS_REQUEST))
    ack = Ack(message_id=message.message_id, accepted=True)
    with pytest.raises(PlatformError) as error:
        commands.acknowledge(cluster.cluster_id, ack, old)
    assert error.value.code == "STALE_AGENT_SESSION"
    with scheduler_db.transaction() as session:
        assert session.get(Command, message.message_id).acknowledged_at is None
    commands.acknowledge(cluster.cluster_id, ack, current)
    assert not commands.pending(cluster.cluster_id)
