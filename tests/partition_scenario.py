"""An executing board, offline completion, and durable queue/cancellation per simulated Mac."""

import asyncio
import time

from fl_agent.connection import SchedulerConnection
from fl_agent.hardware.mock import MockBoardBackend
from fl_common.models import JobConfig, ResourceConstraints
from fl_scheduler.agents.commands import Commands
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.api.control import Control
from fl_scheduler.db.models import Cluster, Event
from fl_scheduler.scheduler.placement import Placement
from sqlalchemy import select

from tests.connected import state, until


async def prepare(service, proxy, db, owner, duration):
    old = service.scheduler_link
    await old.stop()
    link = SchedulerConnection(
        service,
        f"http://127.0.0.1:{proxy.port}",
        old.agent_token,
        allow_http=True,
        heartbeat_seconds=1,
    )
    service.scheduler_link = link
    link.start()
    await until(lambda: link.connected)
    first, second, _ = service.workers.values()
    assert isinstance(first.backend, MockBoardBackend)
    assert isinstance(second.backend, MockBoardBackend)
    first.backend.behavior.run_seconds = duration + 60
    second.backend.behavior.run_seconds = duration / 2
    placement = Placement(db)

    def submit(worker):
        return placement.submit(
            JobConfig(
                resource_constraints=ResourceConstraints(board=worker.board_id),
                run_timeout=duration + 180,
            ),
            owner,
        )

    running, offline = submit(first), submit(second)
    placement.reserve_pending()
    commands = Commands(db)
    for job in (running, offline):
        commands.enqueue_after_transfer(job.job_id)
    await until(lambda: all(state(db, job.job_id) == "RUNNING" for job in (running, offline)))
    # wait_for_completion already owns its current delay; subsequent jobs run promptly.
    first.backend.behavior.run_seconds = 0.02
    queued = []
    for _ in range(2):
        job = submit(first)
        placement.reserve_pending()
        commands.enqueue_after_transfer(job.job_id)
        await until(
            lambda job=job: any(
                record.spec.job_id == job.job_id and record.state == "QUEUED"
                for record in service.db.jobs()
            )
        )
        queued.append(job)
    await until(lambda: not commands.pending(service.config.cluster_id))
    return first, running, offline, queued


async def exercise(service, proxy, db, owner, duration, scenario):
    worker, running, offline, (queued, canceled) = scenario
    await proxy.partition()
    await until(lambda: not service.scheduler_link.connected)

    def disconnected():
        with db.transaction() as session:
            return session.get(Cluster, service.config.cluster_id).current_session is None

    await until(disconnected)
    started = time.monotonic()
    with db.transaction() as session:
        cursor = session.get(Cluster, service.config.cluster_id).event_sequence
    Control(db).cancel(canceled.job_id, owner)
    while (elapsed := time.monotonic() - started) < duration:
        assert not service.scheduler_link.connected
        assert service.db.get(running.job_id).state == "RUNNING" and worker.backend.running
        assert service.db.get(queued.job_id).state == "QUEUED"
        assert service.db.get(canceled.job_id).state == "QUEUED"
        Reconciler(db).health_sweep()
        await asyncio.sleep(min(2, duration - elapsed))
    elapsed = time.monotonic() - started
    assert elapsed >= duration
    assert service.db.get(running.job_id).state == "RUNNING"
    assert service.db.get(offline.job_id).state == "SUCCEEDED"
    assert state(db, offline.job_id) == "RUNNING"  # Scheduler has received no offline result.
    with db.transaction() as session:
        cluster = session.get(Cluster, service.config.cluster_id)
        assert cluster.event_sequence == cursor
        assert cluster.health == ("OFFLINE" if duration == 600 else "DEGRADED")
    proxy.restore()
    await until(lambda: service.scheduler_link.connected, seconds=50)
    await until(lambda: state(db, offline.job_id) == "SUCCEEDED")
    await until(lambda: state(db, canceled.job_id) == "CANCELED")
    await until(
        lambda: all(state(db, job.job_id) == "SUCCEEDED" for job in (running, queued)),
        seconds=100,
    )
    for job in (running, offline, queued):
        events = service.db.job_events(job.job_id)
        assert [event.type for event in events].count("JOB_RUNNING") == 1
        assert [event.type for event in events].count("JOB_SUCCEEDED") == 1
        assert not any(event.type == "JOB_INTERRUPTED" for event in events)
        with db.transaction() as session:
            projected = session.scalars(
                select(Event).where(Event.job_id == job.job_id, Event.type == "JOB_SUCCEEDED")
            ).all()
            assert len(projected) == 1
    assert not service.db.job_events(canceled.job_id, "JOB_RUNNING")
    return {"partition_seconds": duration, "measured_seconds": elapsed, "jobs": 4}
