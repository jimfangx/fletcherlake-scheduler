"""Hundreds of jobs use real PostgreSQL, command delivery and nine durable board queues."""

import asyncio

from fl_agent.hardware.mock import MockBoardBackend
from fl_common.models import JobConfig, ResourceConstraints
from fl_common.models.scheduler import Principal, Role
from fl_scheduler.agents.commands import Commands
from fl_scheduler.api.control import Control
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.db.async_calls import database_call
from fl_scheduler.db.models import Cluster, Command
from fl_scheduler.scheduler.placement import Placement
from fl_scheduler.service import Maintenance
from sqlalchemy import select

from tests.connected import until
from tests.load_workload import all_states, assignments, states, submit_many, verify_execution


async def test_large_workload_priority_placement_cancellation_and_replay(
    scheduler_db,
    connected_agents,
    auth_stack,
    caplog,
):
    _, services = connected_agents
    owner = Principal(email="alice@berkeley.edu", subject="alice", role=Role.USER)
    workers = [worker for service in services for worker in service.workers.values()]
    for worker in workers:
        assert isinstance(worker.backend, MockBoardBackend)
        worker.backend.behavior.hang = True
    placement = Placement(scheduler_db)
    blockers = [
        placement.submit(
            JobConfig(
                resource_constraints=ResourceConstraints(board=worker.board_id),
                run_timeout=600,
            ),
            owner,
        )
        for worker in workers
    ]
    maintenance = Maintenance(
        scheduler_db, AuthAPI(auth_stack[0], auth_stack[1], "https://scheduler.test")
    )
    await database_call(maintenance.tick)
    await until(lambda: all_states(scheduler_db, [job.job_id for job in blockers], {"RUNNING"}))
    specs = await database_call(submit_many, scheduler_db, owner, [w.board_id for w in workers])
    ids = {spec.job_id for spec in specs}
    async with asyncio.timeout(180):
        while not all_states(scheduler_db, ids, {"QUEUED"}):
            # Exercise competing production placement callers; PostgreSQL serializes reservations.
            await asyncio.gather(*[database_call(maintenance.tick) for _ in range(2)])
            await asyncio.sleep(0.05)
    assigned = assignments(scheduler_db, specs)
    commands = Commands(scheduler_db)
    await until(
        lambda: all(not commands.pending(service.config.cluster_id) for service in services)
    )
    target = services[0]
    with scheduler_db.transaction() as session:
        previous = session.get(Cluster, target.config.cluster_id).current_session
    await target.scheduler_link.stop()
    with scheduler_db.transaction() as session:
        replay = list(
            session.scalars(
                select(Command)
                .where(
                    Command.cluster_id == target.config.cluster_id,
                    Command.job_id.in_(ids),
                )
                .limit(7)
            )
        )
        assert len(replay) == 7
        for command in replay:
            command.acknowledged_at = None
    target.scheduler_link.start()
    await until(lambda: target.scheduler_link.connected)
    await until(lambda: not commands.pending(target.config.cluster_id))
    with scheduler_db.transaction() as session:
        assert session.get(Cluster, target.config.cluster_id).current_session != previous
    canceled = {spec.job_id for index, spec in enumerate(specs) if index % 11 == 0}
    control = Control(scheduler_db)
    for job_id in canceled:
        control.cancel(job_id, owner)
    await until(lambda: all_states(scheduler_db, canceled, {"CANCELED"}))
    for worker in workers:
        worker.backend.behavior.hang = False
    for job in blockers:
        control.cancel(job.job_id, owner)
    await until(
        lambda: all(value in {"SUCCEEDED", "CANCELED"} for value in states(scheduler_db).values()),
        seconds=90,
    )
    verify_execution(services, specs, assigned, canceled)
    assert all(states(scheduler_db)[job.job_id] == "CANCELED" for job in blockers)
    assert not any(
        (record.name == "fl_agent.connection" and "COMMAND_BACKLOG" in record.getMessage())
        or (record.name == "uvicorn.error" and record.levelno >= 40)
        for record in caplog.records
    )
