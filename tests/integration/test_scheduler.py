"""Placement and replicated-state tests run against real PostgreSQL, not SQLite."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fl_common.errors import PlatformError
from fl_common.models import JobConfig, ResourceConstraints
from fl_common.models.base import utcnow
from fl_common.models.scheduler import ClusterSnapshot, Principal, Role
from fl_common.protocol import Ack
from fl_scheduler.agents.commands import Commands
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.db.models import Assignment, Cluster, Event, Job
from fl_scheduler.registry import Registry
from fl_scheduler.scheduler.placement import Placement
from sqlalchemy import select


@pytest.fixture
def principal() -> Principal:
    return Principal(email="alice@berkeley.edu", subject="google-sub-alice", role=Role.USER)


@pytest.fixture
def nine_boards(scheduler_db, config):
    registry = Registry(scheduler_db)
    configs = []
    for index in range(3):
        inventory = config.model_copy(deep=True)
        for board in inventory.boards:
            if board is not None:
                board.board_id = f"mac-{index}-{board.board_id}"
        registered = registry.register(inventory, f"test-agent-token-{index}")
        configs.append(registered)
        with scheduler_db.transaction() as session:
            cluster = session.get(Cluster, registered.cluster_id)
            cluster.state, cluster.health, cluster.last_heartbeat = "READY", "ONLINE", utcnow()
    return configs


def test_places_jobs_across_nine_boards_and_reserves_before_staging(
    scheduler_db,
    nine_boards,
    principal,
) -> None:
    placement = Placement(scheduler_db)
    jobs = [
        placement.submit(
            JobConfig(resource_constraints=ResourceConstraints(soc="fletcherlake")), principal
        )
        for _ in range(30)
    ]
    assignments = placement.reserve_pending()
    assert len(assignments) == 9
    assert len({assignment.board_id for assignment in assignments}) == 9
    assert {assignment.cluster_id for assignment in assignments} == {
        config.cluster_id for config in nine_boards
    }
    assert all(assignment.state == "RESERVED" for assignment in assignments)
    assert {assignment.job_id for assignment in assignments} == {job.job_id for job in jobs[:9]}
    staged = placement.begin_staging(assignments[0].job_id, principal)
    assert staged.state == "STAGING"
    other = Principal(email="bob@berkeley.edu", subject="bob", role=Role.USER)
    with pytest.raises(PlatformError, match="another user"):
        placement.begin_staging(assignments[0].job_id, other)


def test_explicit_board_and_niceness_ordering(scheduler_db, nine_boards, principal) -> None:
    placement = Placement(scheduler_db)
    constraint = ResourceConstraints(board="mac-0-board-1", soc="fletcherlake", fpga="xcvu9p")
    low = placement.submit(JobConfig(priority=5, resource_constraints=constraint), principal)
    high = placement.submit(JobConfig(priority=-2, resource_constraints=constraint), principal)
    first = placement.reserve_pending()
    assert len(first) == 1 and first[0].job_id == high.job_id
    assert first[0].board_id == constraint.board
    with scheduler_db.transaction() as session:
        assert session.get(Job, low.job_id).state == "CREATED"


def test_concurrent_reservation_calls_do_not_double_book(
    scheduler_db, nine_boards, principal
) -> None:
    placement = Placement(scheduler_db)
    for _ in range(40):
        placement.submit(JobConfig(), principal)
    with ThreadPoolExecutor(max_workers=2) as pool:
        groups = list(pool.map(lambda _: placement.reserve_pending(), range(2)))
    all_assignments = [assignment for group in groups for assignment in group]
    assert len(all_assignments) == 9
    assert len({assignment.board_id for assignment in all_assignments}) == 9


def test_reservation_expiry_releases_board_but_not_unacknowledged_dispatch(
    scheduler_db,
    nine_boards,
    principal,
) -> None:
    placement = Placement(scheduler_db)
    spec = placement.submit(
        JobConfig(resource_constraints=ResourceConstraints(board="mac-0-board-0")), principal
    )
    first = placement.reserve_pending()[0]
    with scheduler_db.transaction() as session:
        session.get(Assignment, spec.job_id).expires_at = utcnow() - timedelta(seconds=1)
    renewed = placement.reserve_pending()[0]
    assert renewed.job_id == first.job_id
    commands = Commands(scheduler_db)
    message = commands.enqueue_after_transfer(spec.job_id)
    assert commands.enqueue_after_transfer(spec.job_id).message_id == message.message_id
    with scheduler_db.transaction() as session:
        session.get(Assignment, spec.job_id).expires_at = utcnow() - timedelta(seconds=1)
    assert placement.reserve_pending() == []
    with scheduler_db.transaction() as session:
        assert session.get(Assignment, spec.job_id).state == "DELIVERING"
    commands.acknowledge(renewed.cluster_id, Ack(message_id=message.message_id, accepted=True))
    commands.acknowledge(renewed.cluster_id, Ack(message_id=message.message_id, accepted=True))
    assert commands.pending(renewed.cluster_id) == []
    with scheduler_db.transaction() as session:
        assert session.get(Job, spec.job_id).state == "QUEUED"


def test_heartbeats_exclude_stale_clusters_without_killing_jobs(
    scheduler_db, nine_boards, principal
) -> None:
    placement = Placement(scheduler_db)
    job = placement.submit(JobConfig(), principal)
    for config in nine_boards:
        with scheduler_db.transaction() as session:
            session.get(Cluster, config.cluster_id).last_heartbeat = utcnow() - timedelta(
                minutes=10
            )
    Reconciler(scheduler_db).health_sweep()
    assert placement.reserve_pending() == []
    with scheduler_db.transaction() as session:
        assert session.get(Job, job.job_id).state == "CREATED"
        assert all(cluster.health == "OFFLINE" for cluster in session.scalars(select(Cluster)))
        assert len(session.scalars(select(Event).where(Event.type == "CLUSTER_OFFLINE")).all()) == 3


async def test_agent_snapshot_and_event_cursor_reconcile_idempotently(
    scheduler_db,
    config,
    tmp_path,
) -> None:
    from fl_agent.configuration import confirm_config, write_config
    from fl_agent.service import AgentService

    registry = Registry(scheduler_db)
    registered = registry.register(config, "agent-token")
    root = tmp_path / "agent"
    write_config(root, registered)
    confirm_config(root)
    service = AgentService(registered, root, root / "run")
    await service.start()
    reconciler = Reconciler(scheduler_db)
    session_id = reconciler.connected(registered.cluster_id)
    try:
        job = await service.submit(JobConfig())
        snapshot = ClusterSnapshot.model_validate(service.snapshot())
        reconciler.snapshot(registered.cluster_id, session_id, snapshot)
        events = service.db.events()
        cursor = reconciler.events(registered.cluster_id, session_id, events)
        assert reconciler.events(registered.cluster_id, session_id, events) == cursor
        with scheduler_db.transaction() as session:
            assert session.get(Job, job.spec.job_id).state == snapshot.jobs[0].state
            assert session.get(Cluster, registered.cluster_id).event_sequence == cursor
        newer = reconciler.connected(registered.cluster_id)
        reconciler.disconnected(registered.cluster_id, session_id)
        with scheduler_db.transaction() as session:
            assert session.get(Cluster, registered.cluster_id).current_session == newer
        with pytest.raises(PlatformError, match="superseded"):
            reconciler.snapshot(registered.cluster_id, session_id, snapshot)
    finally:
        await service.stop()


def test_registry_never_accepts_wrong_agent_token(scheduler_db, config) -> None:
    registry = Registry(scheduler_db)
    registered = registry.register(config, "correct-token")
    registry.authenticate(registered.cluster_id, "correct-token")
    with pytest.raises(PlatformError, match="invalid"):
        registry.authenticate(registered.cluster_id, "wrong-token")
    with pytest.raises(PlatformError, match="another cluster"):
        registry.register(config, "second-token")


def test_capped_queue_cost_drives_real_placement(scheduler_db, nine_boards, principal) -> None:
    from fl_common.protocol import Ack
    from fl_scheduler.db.models import Board

    placement = Placement(scheduler_db)
    commands = Commands(scheduler_db)
    long_running = placement.submit(
        JobConfig(
            resource_constraints=ResourceConstraints(board="mac-0-board-0"),
            run_timeout=35 * 86400,
        ),
        principal,
    )
    placement.reserve_pending()
    message = commands.enqueue_after_transfer(long_running.job_id)
    commands.acknowledge(
        nine_boards[0].cluster_id, Ack(message_id=message.message_id, accepted=True)
    )
    with scheduler_db.transaction() as session:
        session.get(Job, long_running.job_id).state = "RUNNING"
    for _ in range(10):
        short = placement.submit(
            JobConfig(
                resource_constraints=ResourceConstraints(board="mac-0-board-1"),
                run_timeout=60,
            ),
            principal,
        )
        placement.reserve_pending()
        message = commands.enqueue_after_transfer(short.job_id)
        commands.acknowledge(
            nine_boards[0].cluster_id, Ack(message_id=message.message_id, accepted=True)
        )
    with scheduler_db.transaction() as session:
        for board in session.scalars(select(Board)):
            if board.board_id not in {"mac-0-board-0", "mac-0-board-1"}:
                board.state = "ERROR"
    new = placement.submit(JobConfig(), principal)
    assignment = placement.reserve_pending()[0]
    assert assignment.job_id == new.job_id
    assert assignment.board_id == "mac-0-board-0"
