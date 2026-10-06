"""A revoked node can retry retirement; re-enrollment starts a fresh daemon and history."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from fl.macos.launchd import RETENTION_LABEL
from fl.macos.provisioning import LABEL
from fl_agent.credentials import load_credentials
from fl_agent.db import AgentDB
from fl_agent.retired import sweep_retired
from fl_agent.retired_state import retired_root
from fl_common.errors import PlatformError
from fl_common.models import JobConfig, ResourceConstraints
from fl_common.models.scheduler import ClusterSnapshot
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.db.models import Assignment, Board, Job
from fl_scheduler.db.models import Cluster as ClusterRow

from tests.connected import until


async def test_lost_unregister_after_node_revocation_preserves_history_and_reenrolls(
    retirement_stack,
    scheduler_db,
    config,
    tmp_path,
):
    stack = retirement_stack
    setup, ticket, services = stack.setup, stack.ticket, stack.services
    network, native, joins = stack.network, stack.native, stack.joins
    unregister_requests, calls, server = stack.unregister_requests, stack.calls, stack.server
    admin = stack.admin
    state = setup.state_root
    first = ticket()
    registered = await asyncio.to_thread(
        setup.init,
        config,
        scheduler="https://scheduler.test",
        enrollment_token=first["token"],
    )
    assert (await asyncio.to_thread(setup.confirm, tmp_path))["state"] == "READY"
    old = services[-1]
    running = await old.submit(JobConfig(resource_constraints=ResourceConstraints(board="board-0")))
    await until(lambda: old.db.get(running.spec.job_id).state == "RUNNING")
    queued = await old.submit(JobConfig(resource_constraints=ResourceConstraints(board="board-0")))
    reconciler = Reconciler(scheduler_db)
    session_id = reconciler.connected(registered.cluster_id)
    reconciler.snapshot(
        registered.cluster_id, session_id, ClusterSnapshot.model_validate(old.snapshot())
    )
    with pytest.raises(httpx.ReadError):
        await asyncio.to_thread(setup.destroy)
    assert network.deleted == [joins[0].node_id]
    assert native.loaded(LABEL) and native.loaded(RETENTION_LABEL)
    assert (state / "credentials.json").exists()
    with pytest.raises(PlatformError) as error:
        await asyncio.to_thread(
            setup.init,
            config,
            scheduler="https://scheduler.test",
            enrollment_token=first["token"],
        )
    assert error.value.code == "DESTROY_PENDING"
    final = await asyncio.to_thread(setup.destroy)
    assert final.state == "DESTROYED" and len(unregister_requests) == 2
    assert not native.loaded(LABEL) and native.loaded(RETENTION_LABEL)
    assert not state.exists() and calls.count("logout") == 1
    archive = retired_root(state) / str(registered.cluster_id)
    old_db = AgentDB(archive / "agent.db")
    try:
        assert old_db.get(running.spec.job_id).state == "INTERRUPTED"
        assert old_db.get(queued.spec.job_id).state == "CANCELED"
    finally:
        old_db.close()
    second = ticket()
    replacement = await asyncio.to_thread(
        setup.init,
        config,
        scheduler="https://scheduler.test",
        enrollment_token=second["token"],
    )
    assert replacement.cluster_id != registered.cluster_id and len(joins) == 2
    assert (await asyncio.to_thread(setup.confirm, tmp_path))["state"] == "READY"
    current = services[-1]
    assert current.db.jobs() == [] and current.state == "READY"
    assert all(worker.backend.calls.count("start") == 0 for worker in current.workers.values())
    fresh = await current.submit(
        JobConfig(resource_constraints=ResourceConstraints(board="board-1"))
    )
    await until(lambda: current.db.get(fresh.spec.job_id).state == "SUCCEEDED")
    fresh_session = reconciler.connected(replacement.cluster_id)
    reconciler.snapshot(
        replacement.cluster_id,
        fresh_session,
        ClusterSnapshot.model_validate(current.snapshot()),
    )
    expiry = max(record.expires_at for record in final.artifacts) + timedelta(seconds=1)
    report = sweep_retired(retired_root(state), now=expiry)
    assert report.deleted == 2 and report.failed == 0
    assert current.store.path(fresh.spec.job_id, "results").is_file()
    assert current.db.get(fresh.spec.job_id).state == "SUCCEEDED"
    with scheduler_db.transaction() as session:
        assert session.get(ClusterRow, registered.cluster_id).state == "DESTROYED"
        assert session.get(ClusterRow, replacement.cluster_id).state == "READY"
        assert session.get(Job, running.spec.job_id).state == "INTERRUPTED"
        assert session.get(Job, fresh.spec.job_id).state == "SUCCEEDED"
        assert session.get(Assignment, running.spec.job_id).cluster_id == registered.cluster_id
        assert session.get(Assignment, queued.spec.job_id).cluster_id == registered.cluster_id
        assert session.get(Assignment, fresh.spec.job_id).cluster_id == replacement.cluster_id
        for board in replacement.boards:
            if board:
                assert session.get(Board, board.board_id).cluster_id == replacement.cluster_id
    new_credentials = load_credentials(state / "credentials.json")
    for token in (
        second["token"],
        admin.access_token.get_secret_value(),
        new_credentials.agent_token.get_secret_value(),
    ):
        response = server.post(
            unregister_requests[0],
            json=final.model_dump(mode="json"),
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 401
