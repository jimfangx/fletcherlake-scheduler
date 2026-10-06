"""Only acknowledged idle retirement releases physical boards; old assignments stay historical."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fl_common.errors import PlatformError
from fl_common.models import JobConfig, JobSpec
from fl_common.models.base import utcnow
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.db.models import Assignment, Board, Cluster, Event, Job
from fl_scheduler.registry import Registry
from sqlalchemy import select


@pytest.mark.parametrize(
    "blocked",
    ["live", "draining", "powered_off", "unacknowledged", "active_board", "active_assignment"],
)
def test_retired_board_claim_requires_full_retirement_and_no_active_work(
    scheduler_db, config, blocked
):
    registry = Registry(scheduler_db)
    previous = registry.register(config, "old-token")
    with scheduler_db.transaction(placement=True) as session:
        cluster = session.get(Cluster, previous.cluster_id)
        cluster.state = {"live": "READY", "draining": "DRAINING", "powered_off": "POWERED_OFF"}.get(
            blocked, "DESTROYED"
        )
        for board in session.scalars(select(Board)):
            board.enabled = False
        if blocked != "unacknowledged":
            session.add(
                Event(cluster_id=previous.cluster_id, type="CLUSTER_UNREGISTERED", payload={})
            )
        if blocked == "active_board":
            session.get(Board, "board-0").active_job = uuid4()
        if blocked == "active_assignment":
            spec = JobSpec.from_config(JobConfig(), "alice")
            session.add(
                Job(
                    job_id=spec.job_id,
                    owner=spec.owner,
                    spec=spec.model_dump(mode="json"),
                    priority=0,
                    submitted_at=spec.submitted_at,
                )
            )
            session.flush()
            session.add(
                Assignment(
                    job_id=spec.job_id,
                    cluster_id=previous.cluster_id,
                    board_id="board-0",
                    state="QUEUED",
                    expires_at=utcnow(),
                )
            )
    with pytest.raises(PlatformError) as error:
        registry.register(config, "new-token")
    assert error.value.code == "BOARD_ID_CONFLICT"
    with scheduler_db.transaction() as session:
        assert len(session.scalars(select(Cluster)).all()) == 1
        assert all(
            board.cluster_id == previous.cluster_id for board in session.scalars(select(Board))
        )


def test_concurrent_reenrollment_claims_one_owner_and_records_transfer(scheduler_db, config):
    registry = Registry(scheduler_db)
    previous = registry.register(config, "old-token")
    with scheduler_db.transaction(placement=True) as session:
        session.get(Cluster, previous.cluster_id).state = "DESTROYED"
        for board in session.scalars(select(Board)):
            board.enabled = False
        session.add(Event(cluster_id=previous.cluster_id, type="CLUSTER_UNREGISTERED", payload={}))

    def claim(token):
        try:
            return registry.register(config, token)
        except PlatformError as error:
            assert error.code == "BOARD_ID_CONFLICT"
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, ["new-token-1", "new-token-2"]))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    with scheduler_db.transaction() as session:
        assert len(session.scalars(select(Cluster)).all()) == 2
        assert all(
            board.cluster_id == winners[0].cluster_id for board in session.scalars(select(Board))
        )
        events = session.scalars(select(Event).where(Event.type == "BOARD_REASSIGNED")).all()
        assert len(events) == 3
        assert all(
            event.payload["previous_cluster_id"] == str(previous.cluster_id) for event in events
        )


def test_authenticated_connection_cannot_resurrect_retired_inventory(scheduler_db, config):
    registry = Registry(scheduler_db)
    registered = registry.register(config, "old-token")
    reconciler = Reconciler(scheduler_db)
    old_session = reconciler.connected(registered.cluster_id)
    registry.authenticate(registered.cluster_id, "old-token")  # Authentication preceded retirement.
    with scheduler_db.transaction(placement=True) as session:
        cluster = session.get(Cluster, registered.cluster_id)
        cluster.state, cluster.current_session, cluster.health = "DESTROYED", None, "OFFLINE"
        for board in session.scalars(select(Board)):
            board.enabled = False
    for action in (
        lambda: reconciler.connected(registered.cluster_id),
        lambda: registry.update_config(registered.cluster_id, registered, session_id=old_session),
    ):
        with pytest.raises(PlatformError) as error:
            action()
        assert error.value.code == "UNAUTHORIZED_AGENT"
    with scheduler_db.transaction() as session:
        cluster = session.get(Cluster, registered.cluster_id)
        assert cluster.current_session is None and cluster.state == "DESTROYED"
        assert all(not board.enabled for board in session.scalars(select(Board)))


def test_superseded_hello_cannot_change_current_inventory(scheduler_db, config):
    registry = Registry(scheduler_db)
    registered = registry.register(config, "old-token")
    reconciler = Reconciler(scheduler_db)
    first = reconciler.connected(registered.cluster_id)
    current = reconciler.connected(registered.cluster_id)
    changed = registered.model_copy(deep=True)
    changed.boards[0] = None
    changed.apple_model = "stale inventory"
    with pytest.raises(PlatformError) as error:
        registry.update_config(registered.cluster_id, changed, session_id=first)
    assert error.value.code == "STALE_SESSION"
    with scheduler_db.transaction() as session:
        assert session.get(Cluster, registered.cluster_id).current_session == current
        assert (
            session.get(Cluster, registered.cluster_id).config["apple_model"] == config.apple_model
        )
        assert session.get(Board, "board-0").enabled
