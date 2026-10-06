"""Final unregistration cancels work not received by the Mac and rejects missing queued work."""

import asyncio

import pytest
from fl_agent.retired_state import retired_root
from fl_agent.retirement import Retirement
from fl_common.errors import PlatformError
from fl_common.models import JobConfig, ResourceConstraints
from fl_common.models.scheduler import ClusterSnapshot, Principal, Role
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.db.models import Assignment, Board, Cluster, Event, Job
from fl_scheduler.scheduler.placement import Placement
from sqlalchemy import select


@pytest.mark.parametrize("phase", ["RESERVED", "STAGING", "DELIVERING", "EXPIRED", "QUEUED"])
async def test_unregister_finishes_only_undelivered_assignment_phases(
    retirement_stack,
    scheduler_db,
    config,
    tmp_path,
    phase,
):
    stack = retirement_stack
    stack.controls["lose_unregister"] = False
    ticket = stack.ticket()
    registered = await asyncio.to_thread(
        stack.setup.init,
        config,
        scheduler="https://scheduler.test",
        enrollment_token=ticket["token"],
    )
    await asyncio.to_thread(stack.setup.confirm, tmp_path)
    current = stack.services[-1]
    reconciler = Reconciler(scheduler_db)
    session_id = reconciler.connected(registered.cluster_id)
    reconciler.snapshot(
        registered.cluster_id, session_id, ClusterSnapshot.model_validate(current.snapshot())
    )
    placement = Placement(scheduler_db)
    job = placement.submit(
        JobConfig(resource_constraints=ResourceConstraints(board="board-2")),
        Principal(email="alice@example.edu", subject="alice", role=Role.USER),
    )
    reservations = placement.reserve_pending()
    assert len(reservations) == 1 and reservations[0].job_id == job.job_id
    with scheduler_db.transaction(placement=True) as session:
        session.get(Assignment, job.job_id).state = phase
        if phase == "QUEUED":
            session.get(Job, job.job_id).state = "QUEUED"
    assert current.db.jobs() == []
    if phase == "QUEUED":
        with pytest.raises(PlatformError) as error:
            await asyncio.to_thread(stack.setup.destroy)
        assert error.value.code == "CLUSTER_NOT_DRAINED"
        assert Retirement.load(retired_root(stack.setup.state_root)).phase == "DRAINED"
        assert current.lock.held and stack.setup.state_root.exists()
        with scheduler_db.transaction() as session:
            assert session.get(Cluster, registered.cluster_id).state == "READY"
            assert session.get(Assignment, job.job_id).state == "QUEUED"
            assert not session.get(Job, job.job_id).cancel_requested
            assert all(board.enabled for board in session.scalars(select(Board)))
            assert not session.scalar(select(Event).where(Event.type == "CLUSTER_UNREGISTERED"))
    else:
        await asyncio.to_thread(stack.setup.destroy)
        with scheduler_db.transaction() as session:
            assert session.get(Assignment, job.job_id).state == "FINISHED"
            row = session.get(Job, job.job_id)
            assert row.state == "CANCELED" and row.cancel_requested
            events = session.scalars(
                select(Event).where(Event.type == "JOB_CANCELED", Event.job_id == job.job_id)
            ).all()
            assert len(events) == 1 and events[0].payload == {"reason": "CLUSTER_UNREGISTERED"}
            assert all(not board.enabled for board in session.scalars(select(Board)))
