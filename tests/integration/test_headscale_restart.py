"""Real coordinator/relay restart must preserve local execution, identity and replay."""

import asyncio
import json
import time

import httpx
import pytest
from fl_agent.hardware.base import RunResult
from fl_agent.hardware.mock import MockBoardBackend
from fl_common.errors import PlatformError
from fl_common.files import atomic_write
from fl_common.models import JobConfig, ResourceConstraints
from fl_common.models.scheduler import Principal, Role
from fl_scheduler.agents.commands import Commands
from fl_scheduler.api.control import Control
from fl_scheduler.db.models import Cluster, Event
from fl_scheduler.scheduler.placement import Placement
from sqlalchemy import select

from tests.connected import state, until


def node_identities(coordinator):
    response = httpx.get(
        coordinator.url + "/api/v1/node",
        headers={"Authorization": "Bearer " + coordinator.api_key},
        timeout=5,
        trust_env=False,
    )
    assert response.status_code == 200
    return {
        node["id"]: (
            node["nodeKey"],
            tuple(sorted(node["ipAddresses"])),
            tuple(sorted(node["tags"])),
        )
        for node in response.json()["nodes"]
    }


def projection(db, cluster_id):
    with db.transaction() as session:
        cluster = session.get(Cluster, cluster_id)
        return cluster.event_sequence, cluster.current_session


def assert_private_dns(network):
    for role in ("scheduler", "gateway"):
        reply = json.loads(
            network.peers["mac"].command("dns", "query", "--json", f"{role}.fl.internal", "A")
        )
        assert reply["ResponseCode"] == "RCodeSuccess"
        assert [answer["Body"] for answer in reply["Answers"] if answer["Type"] == "TypeA"] == [
            network.addresses[role]
        ]


@pytest.mark.parametrize("tailscale_peers", [True], indirect=True, ids=["derp-only"])
async def test_headscale_restart_preserves_hardware_and_reconciles_offline_work(
    scheduler_db, private_stack, headscale_process, monkeypatch, tmp_path
):
    network = private_stack
    service = network.agent
    cluster_id = service.config.cluster_id
    first, second, _ = service.workers.values()
    assert isinstance(first.backend, MockBoardBackend)
    assert isinstance(second.backend, MockBoardBackend)

    def hold_completion(backend):
        finished = asyncio.Event()

        async def complete_when_released():
            await finished.wait()
            return RunResult(passed=True)

        monkeypatch.setattr(backend, "wait_for_completion", complete_when_released)
        return finished

    finished, offline_finished = hold_completion(first.backend), hold_completion(second.backend)
    owner = Principal(email="alice@berkeley.edu", subject="alice", role=Role.USER)
    placement, commands = Placement(scheduler_db), Commands(scheduler_db)

    async def submit(worker, expected):
        spec = placement.submit(
            JobConfig(
                resource_constraints=ResourceConstraints(board=worker.board_id), run_timeout=180
            ),
            owner,
        )
        placement.reserve_pending()
        commands.enqueue_after_transfer(spec.job_id)
        await until(
            lambda: any(
                record.spec.job_id == spec.job_id and record.state == expected
                for record in service.db.jobs()
            )
        )
        return spec

    async with network.control.client:
        running = await submit(first, "RUNNING")
        offline = await submit(second, "RUNNING")
        queued = await submit(first, "QUEUED")
        canceled = await submit(first, "QUEUED")
        await until(lambda: not commands.pending(cluster_id))
        await until(lambda: projection(scheduler_db, cluster_id)[0] == service.db.events()[-1].seq)
        assert (await network.control.request("GET", "/healthz")).status_code == 200
        await asyncio.to_thread(assert_private_dns, network)
        identities = await asyncio.to_thread(node_identities, headscale_process)
        assert len(identities) == 3
        old_cursor, old_session = projection(scheduler_db, cluster_id)
        peer_processes = {name: peer.process.pid for name, peer in network.peers.items()}
        coordinator_pid = headscale_process.process.pid
        stops = first.backend.calls.count("stop")
        await asyncio.to_thread(headscale_process.stop)
        assert headscale_process.process.poll() is not None
        outage_started = time.monotonic()
        try:
            with pytest.raises(PlatformError) as error:
                await network.control.request("GET", "/healthz", http_timeout=1)
            assert error.value.code == "TRANSFER_GATEWAY_UNAVAILABLE"
            offline_finished.set()
            Control(scheduler_db).cancel(canceled.job_id, owner)
            await until(lambda: not service.scheduler_link.connected, seconds=50)
            assert first.backend.running and service.db.get(running.job_id).state == "RUNNING"
            assert service.db.get(offline.job_id).state == "SUCCEEDED"
            assert state(scheduler_db, offline.job_id) == "RUNNING"
            assert projection(scheduler_db, cluster_id)[0] == old_cursor
            assert service.db.get(queued.job_id).state == "QUEUED"
            assert service.db.get(canceled.job_id).state == "QUEUED"
            assert first.backend.calls.count("stop") == stops
            await asyncio.to_thread(assert_private_dns, network)
            assert network.server.server.started and not network.server.task.done()
        finally:
            await asyncio.to_thread(headscale_process.start)
        outage_seconds = time.monotonic() - outage_started
        assert outage_seconds >= 10
        assert headscale_process.process.pid != coordinator_pid
        assert await asyncio.to_thread(node_identities, headscale_process) == identities
        await asyncio.to_thread(assert_private_dns, network)
        assert all(
            peer.process.poll() is None and peer.process.pid == peer_processes[name]
            for name, peer in network.peers.items()
        )
        await until(lambda: service.scheduler_link.connected, seconds=60)
        assert projection(scheduler_db, cluster_id)[1] != old_session
        await until(lambda: state(scheduler_db, offline.job_id) == "SUCCEEDED")
        await until(lambda: state(scheduler_db, canceled.job_id) == "CANCELED")
        assert first.backend.running
        finished.set()
        await until(
            lambda: all(
                state(scheduler_db, spec.job_id) == "SUCCEEDED" for spec in (running, queued)
            )
        )
        # Job metadata can arrive in a snapshot before its durable event batch.
        await until(lambda: projection(scheduler_db, cluster_id)[0] == service.db.events()[-1].seq)
        await until(lambda: not commands.pending(cluster_id))
        for spec in (running, offline, queued):
            events = [event.type for event in service.db.job_events(spec.job_id)]
            assert events.count("JOB_RUNNING") == events.count("JOB_SUCCEEDED") == 1
            assert "JOB_INTERRUPTED" not in events
            with scheduler_db.transaction() as session:
                assert (
                    len(
                        session.scalars(
                            select(Event).where(
                                Event.job_id == spec.job_id, Event.type == "JOB_SUCCEEDED"
                            )
                        ).all()
                    )
                    == 1
                )
        assert not service.db.job_events(canceled.job_id, "JOB_RUNNING")
        assert not commands.pending(cluster_id)
        for address in (network.addresses["scheduler"], network.addresses["gateway"]):
            await asyncio.to_thread(network.peers["mac"].wait_connection, address)
            network.peers["mac"].assert_relay(address)
        assert (await network.control.request("GET", "/healthz")).status_code == 200
        atomic_write(
            tmp_path / "headscale-restart-report.json",
            json.dumps(
                {
                    "coordinator_outage_seconds": outage_seconds,
                    "preserved_nodes": len(identities),
                    "successful_jobs": [str(spec.job_id) for spec in (running, offline, queued)],
                    "canceled_job": str(canceled.job_id),
                    "transport": "derp-only",
                    "private_dns": "verified before, during and after coordinator outage",
                },
                indent=2,
            ).encode(),
        )
