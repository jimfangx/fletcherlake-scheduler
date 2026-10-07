"""Real Rclone + HTTP control + PostgreSQL + WebSocket delivery gates physical execution."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from fl_common.errors import PlatformError
from fl_common.models import JobConfig, ResourceConstraints
from fl_common.models.base import utcnow
from fl_common.models.scheduler import Principal, Role
from fl_common.rclone import Rclone
from fl_gateway.api import create_app
from fl_scheduler.agents.commands import Commands
from fl_scheduler.api.control import Control
from fl_scheduler.db.models import Assignment, Command
from fl_scheduler.scheduler.placement import Placement
from fl_scheduler.transfers.gateway import GatewayControl
from fl_scheduler.transfers.models import Delivery
from fl_scheduler.transfers.service import TransferService
from pydantic import SecretStr
from sqlalchemy import select

from tests.connected import state, until
from tests.transfer import TOKEN_HASH, grant

CONTROL = SecretStr("private scheduler gateway credential for tests")
OWNER = Principal(email="alice@berkeley.edu", subject="alice", role=Role.USER)


def delivery_state(db, job_id):
    with db.transaction() as session:
        row = session.get(Delivery, job_id)
        return row.state if row else None


async def setup_delivery(scheduler_db, connected_agents, rclone_gateway, tmp_path):
    _, agents = connected_agents
    store, endpoint, binary = rclone_gateway
    data = b"actual remote collateral payload\n" * 8192
    upload, identity = grant(tmp_path, data)
    placement = Placement(scheduler_db)
    target = agents[0]
    board_id = next(iter(target.workers))
    spec = placement.submit(
        JobConfig(binary="binary.elf", resource_constraints=ResourceConstraints(board=board_id)),
        OWNER,
        binary=upload.files[0],
    )
    upload.job_id = spec.job_id
    placement.reserve_pending()
    store.register(upload)
    source = tmp_path / "user payload.elf"
    source.write_bytes(data)
    await Rclone(str(binary)).copy(source, endpoint, upload, "binary", identity)
    store.verify(upload.transfer_id, TOKEN_HASH)
    target.transfers.transport = Rclone(str(binary))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, CONTROL)),
        base_url="https://gateway.test",
    )
    coordinator = TransferService(
        scheduler_db,
        GatewayControl("https://gateway.test", CONTROL, client),
        endpoint,
        simulation_networks=("127.0.0.1/32",),
    )
    return spec, upload, coordinator, client, target, store


async def test_verified_delivery_resumes_after_scheduler_worker_restart(
    scheduler_db,
    connected_agents,
    rclone_gateway,
    tmp_path,
):
    spec, upload, coordinator, client, target, store = await setup_delivery(
        scheduler_db, connected_agents, rclone_gateway, tmp_path
    )
    try:
        transfer_id = await coordinator.begin(spec.job_id, upload.transfer_id, OWNER)
        assert await coordinator.begin(spec.job_id, upload.transfer_id, OWNER) == transfer_id
        # An elapsed ordinary reservation cannot relocate an acknowledged delivery.
        with scheduler_db.transaction() as session:
            session.get(Assignment, spec.job_id).expires_at = utcnow() - timedelta(seconds=1)
        Placement(scheduler_db).reserve_pending()
        await until(lambda: any(job.spec.job_id == spec.job_id for job in target.db.jobs()))
        assert target.db.get(spec.job_id).state == "STAGING"
        assert not any(event.type == "JOB_RUNNING" for event in target.db.job_events(spec.job_id))
        with pytest.raises(PlatformError) as error:
            Commands(scheduler_db).enqueue_after_transfer(spec.job_id)
        assert error.value.code == "RESERVATION_EXPIRED"
        replacement = TransferService(
            scheduler_db,
            coordinator.gateway,
            coordinator.endpoint,
            simulation_networks=coordinator.simulation_networks,
        )
        operation = asyncio.create_task(replacement.run())
        try:
            await until(lambda: state(scheduler_db, spec.job_id) == "SUCCEEDED", seconds=20)
            await until(lambda: delivery_state(scheduler_db, spec.job_id) == "DONE", seconds=10)
        finally:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        assert target.db.get(spec.job_id).state == "SUCCEEDED"
        assert sum(event.type == "JOB_RUNNING" for event in target.db.job_events(spec.job_id)) == 1
        assert store.lookup(transfer_id, active=False)[1] == "REVOKED"
        with scheduler_db.transaction() as session:
            kinds = [
                command.envelope["type"]
                for command in session.scalars(select(Command).where(Command.job_id == spec.job_id))
            ]
        assert (
            kinds.count("JOB_STAGE") == kinds.count("JOB_FETCH") == kinds.count("JOB_ENQUEUE") == 1
        )
    finally:
        await client.aclose()


async def test_cancel_after_staging_prevents_fetch_and_enqueue(
    scheduler_db,
    connected_agents,
    rclone_gateway,
    tmp_path,
):
    spec, upload, coordinator, client, target, store = await setup_delivery(
        scheduler_db, connected_agents, rclone_gateway, tmp_path
    )
    try:
        transfer_id = await coordinator.begin(spec.job_id, upload.transfer_id, OWNER)
        await until(lambda: any(job.spec.job_id == spec.job_id for job in target.db.jobs()))
        Control(scheduler_db).cancel(spec.job_id, OWNER)
        await coordinator.tick()
        await until(lambda: state(scheduler_db, spec.job_id) == "CANCELED")
        await coordinator.advance(spec.job_id)
        assert delivery_state(scheduler_db, spec.job_id) == "DONE"
        with scheduler_db.transaction() as session:
            kinds = [
                command.envelope["type"]
                for command in session.scalars(select(Command).where(Command.job_id == spec.job_id))
            ]
        assert "JOB_FETCH" not in kinds and "JOB_ENQUEUE" not in kinds
        assert not any(event.type == "JOB_RUNNING" for event in target.db.job_events(spec.job_id))
        assert not (store.root / "authorized_keys").read_text().count(str(transfer_id))
    finally:
        await client.aclose()


async def test_delivery_authorizes_owner_before_gateway_probe(
    scheduler_db,
    connected_agents,
    rclone_gateway,
    tmp_path,
):
    spec, upload, coordinator, client, _, _ = await setup_delivery(
        scheduler_db, connected_agents, rclone_gateway, tmp_path
    )
    try:
        bob = Principal(email="bob@berkeley.edu", subject="bob", role=Role.USER)
        with pytest.raises(PlatformError) as error:
            await coordinator.begin(spec.job_id, upload.transfer_id, bob)
        assert error.value.code == "FORBIDDEN"
        assert delivery_state(scheduler_db, spec.job_id) is None
    finally:
        await client.aclose()
