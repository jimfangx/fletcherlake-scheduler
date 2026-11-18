"""Real TCP/WebSocket + PostgreSQL + three Mac simulators, without SSH execution."""

import threading
from pathlib import Path

import pytest
from fl_common.models import JobConfig
from fl_common.models.scheduler import Principal, Role
from fl_common.protocol import Message, MessageType
from fl_scheduler.agents.commands import Commands
from fl_scheduler.db.async_calls import database_call
from fl_scheduler.db.models import Cluster, Command, Event, Job
from fl_scheduler.scheduler.placement import Placement
from sqlalchemy import select

from tests.connected import GatewayServer, state, until


@pytest.mark.parametrize("delay_events", [False, True], ids=["normal", "delayed-events"])
async def test_scheduler_to_nine_boards_to_replicated_results(
    scheduler_db,
    connected_agents,
    monkeypatch,
    delay_events,
) -> None:
    server, services = connected_agents
    principal = Principal(email="alice@berkeley.edu", subject="alice", role=Role.USER)
    placement = Placement(scheduler_db)
    specs = [placement.submit(JobConfig(), principal) for _ in range(9)]
    assigned = placement.reserve_pending()
    assert len(assigned) == 9
    release_events, events_blocked = threading.Event(), threading.Event()
    if delay_events:
        # Hold event projection only after a cluster's final job snapshot commits.
        # This exposes the interval where SUCCEEDED is visible but its events aren't.
        completed = {service.config.cluster_id: threading.Event() for service in services}
        expected_jobs = {
            cluster_id: {item.job_id for item in assigned if item.cluster_id == cluster_id}
            for cluster_id in completed
        }
        reconciler = server.gateway.reconciler
        original_snapshot, original_events = reconciler.snapshot, reconciler.events

        def snapshot(cluster_id, session_id, value):
            original_snapshot(cluster_id, session_id, value)
            finished = {record.spec.job_id for record in value.jobs if record.state == "SUCCEEDED"}
            if expected_jobs[cluster_id] <= finished:
                completed[cluster_id].set()

        def events(cluster_id, session_id, value):
            if completed[cluster_id].is_set():
                events_blocked.set()
                assert release_events.wait(15), "Event projection was not released"
            return original_events(cluster_id, session_id, value)

        monkeypatch.setattr(reconciler, "snapshot", snapshot)
        monkeypatch.setattr(reconciler, "events", events)
    commands = Commands(scheduler_db)
    for spec in specs:
        commands.enqueue_after_transfer(spec.job_id)
    try:
        await until(
            lambda: all(state(scheduler_db, spec.job_id) == "SUCCEEDED" for spec in specs),
            poll_in_thread=True,
            description="all nine job snapshots completed",
        )
        # Capture local cursors on the agent's event loop; PostgreSQL polling can run
        # in a worker without reading the agents' SQLite connections from that thread.
        expected_cursors = {
            service.config.cluster_id: service.db.last_sequence() for service in services
        }
        assert all(sequence > 0 for sequence in expected_cursors.values())

        def events_replicated():
            with scheduler_db.transaction() as session:
                cursors = dict(
                    session.execute(select(Cluster.cluster_id, Cluster.event_sequence)).all()
                )
            return all(
                cursors.get(cluster_id, -1) >= sequence
                for cluster_id, sequence in expected_cursors.items()
            )

        if delay_events:
            await until(events_blocked.is_set, description="event projection held after snapshots")
            assert not await database_call(events_replicated)
    finally:
        # Also unblock projection if an earlier assertion fails, allowing clean teardown.
        release_events.set()
    await until(events_replicated, poll_in_thread=True, description="all agent events replicated")
    assert {job.board_id for service in services for job in service.db.jobs()} == {
        assignment.board_id for assignment in assigned
    }
    assert all(
        job.spec.owner == principal.email for service in services for job in service.db.jobs()
    )
    await until(
        lambda: all(not commands.pending(service.config.cluster_id) for service in services),
        poll_in_thread=True,
        description="all enqueue commands acknowledged",
    )
    with scheduler_db.transaction() as session:
        assert all(cluster.event_sequence > 0 for cluster in session.scalars(select(Cluster)))
        assert all(job.record["state"] == "SUCCEEDED" for job in session.scalars(select(Job)))
        succeeded = list(session.scalars(select(Event.job_id).where(Event.type == "JOB_SUCCEEDED")))
        assert len(succeeded) == 9 and set(succeeded) == {spec.job_id for spec in specs}


async def test_command_replay_does_not_execute_same_job_twice(
    scheduler_db, connected_agents
) -> None:
    _, services = connected_agents
    principal = Principal(email="alice@berkeley.edu", subject="alice", role=Role.USER)
    placement = Placement(scheduler_db)
    spec = placement.submit(JobConfig(), principal)
    placement.reserve_pending()
    commands = Commands(scheduler_db)
    message = commands.enqueue_after_transfer(spec.job_id)
    await until(lambda: state(scheduler_db, spec.job_id) == "SUCCEEDED")
    with scheduler_db.transaction() as session:
        session.get(Command, message.message_id).acknowledged_at = None
    await until(
        lambda: all(not commands.pending(service.config.cluster_id) for service in services)
    )
    events = [
        event
        for service in services
        for event in service.db.events()
        if event.job_id == spec.job_id
    ]
    assert [event.type for event in events].count("JOB_RUNNING") == 1
    assert [event.type for event in events].count("JOB_SUCCEEDED") == 1


async def test_scheduler_restart_does_not_interrupt_hardware(
    scheduler_db, connected_agents
) -> None:
    from fl_agent.hardware.mock import MockBoardBackend

    server, services = connected_agents
    target = services[0]
    worker = next(iter(target.workers.values()))
    board = worker.backend
    assert isinstance(board, MockBoardBackend)
    board.behavior.run_seconds = 4
    principal = Principal(email="alice@berkeley.edu", subject="alice", role=Role.USER)
    from fl_common.models import ResourceConstraints

    placement = Placement(scheduler_db)
    spec = placement.submit(
        JobConfig(resource_constraints=ResourceConstraints(board=worker.board_id)), principal
    )
    placement.reserve_pending()
    commands = Commands(scheduler_db)
    commands.enqueue_after_transfer(spec.job_id)
    await until(lambda: state(scheduler_db, spec.job_id) == "RUNNING")
    await server.stop()
    assert board.running
    assert target.db.get(spec.job_id).state == "RUNNING"
    replacement = GatewayServer(scheduler_db, port=server.port)
    await replacement.start()
    try:
        await until(lambda: state(scheduler_db, spec.job_id) == "SUCCEEDED")
        assert target.db.get(spec.job_id).state == "SUCCEEDED"
        events = [event.type for event in target.db.events() if event.job_id == spec.job_id]
        assert "JOB_INTERRUPTED" not in events
    finally:
        await replacement.stop()


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("failure", "FAILED"),
        ("timeout", "TIMED_OUT"),
        ("cancel", "CANCELED"),
    ],
)
async def test_execution_outcomes_replicate_through_gateway(
    scheduler_db,
    connected_agents,
    mode: str,
    expected: str,
) -> None:
    from fl_agent.hardware.mock import MockBoardBackend
    from fl_common.models import ResourceConstraints

    _, services = connected_agents
    target = services[0]
    worker = next(iter(target.workers.values()))
    assert isinstance(worker.backend, MockBoardBackend)
    worker.backend.behavior.passed = mode != "failure"
    worker.backend.behavior.hang = mode in {"timeout", "cancel"}
    principal = Principal(email="alice@berkeley.edu", subject="alice", role=Role.USER)
    placement = Placement(scheduler_db)
    spec = placement.submit(
        JobConfig(
            resource_constraints=ResourceConstraints(board=worker.board_id),
            run_timeout=1 if mode == "timeout" else 10,
        ),
        principal,
    )
    placement.reserve_pending()
    commands = Commands(scheduler_db)
    commands.enqueue_after_transfer(spec.job_id)
    if mode == "cancel":
        await until(lambda: state(scheduler_db, spec.job_id) == "RUNNING")
        commands.issue(
            target.config.cluster_id,
            Message(
                type=MessageType.JOB_CANCEL,
                payload={"job_id": str(spec.job_id)},
            ),
            spec.job_id,
        )
    await until(lambda: state(scheduler_db, spec.job_id) == expected)
    assert target.db.get(spec.job_id).state == expected
    assert not worker.backend.running


async def test_staging_reserves_then_verified_inputs_execute(
    scheduler_db,
    connected_agents,
    input_files,
) -> None:
    from fl_agent.files import sha256_file
    from fl_common.models import ArtifactRef, ResourceConstraints

    _, services = connected_agents
    target = services[0]
    board_id = next(iter(target.workers))
    principal = Principal(email="alice@berkeley.edu", subject="alice", role=Role.USER)
    refs = {}
    for kind, source in input_files.items():
        digest, size = sha256_file(Path(source))
        refs[kind] = ArtifactRef(kind=kind, sha256=digest, size_bytes=size)
    placement = Placement(scheduler_db)
    spec = placement.submit(
        JobConfig(
            **input_files,
            resource_constraints=ResourceConstraints(board=board_id),
        ),
        principal,
        **refs,
    )
    placement.reserve_pending()
    placement.begin_staging(spec.job_id, principal)
    commands = Commands(scheduler_db)
    stage = Message(
        type=MessageType.JOB_STAGE,
        payload={
            "spec": spec.model_dump(mode="json"),
            "board_id": board_id,
        },
    )
    commands.issue(target.config.cluster_id, stage, spec.job_id)
    await until(lambda: any(job.spec.job_id == spec.job_id for job in target.db.jobs()))
    assert target.db.get(spec.job_id).state == "STAGING"
    from fl_scheduler.db.models import Assignment

    with scheduler_db.transaction() as session:
        assert session.get(Assignment, spec.job_id).state == "STAGING"
    # Inject bytes here; the gateway/Rclone transport remains separate implementation work.
    for kind, source in input_files.items():
        target.store.copy_input(spec.job_id, kind, Path(source))
    commands.enqueue_after_transfer(spec.job_id)
    await until(lambda: state(scheduler_db, spec.job_id) == "SUCCEEDED")
    phases = [event.type for event in target.db.events() if event.job_id == spec.job_id]
    assert "JOB_PROGRAMMING_FPGA" in phases
    assert "JOB_PROGRAMMING_SOC" in phases
