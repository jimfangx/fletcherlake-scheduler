"""Native Mac warnings and scheduler downtime/retention events feed independent consumers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from fl_agent.hardware.mock import MockBehavior, MockBoardBackend
from fl_common.models import JobConfig, ResourceConstraints
from fl_common.models.base import utcnow
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.db.models import Event, Notification
from fl_scheduler.notifications.alerts import Alerts
from fl_scheduler.notifications.service import Notifications
from sqlalchemy import select

from tests.connected import until
from tests.integration.test_artifact_downloads import OWNER, completed
from tests.notification_helpers import make_due, settings, success


@pytest.mark.parametrize("outcome", ["hang", "success", "cancel"])
async def test_agent_warns_on_consumed_deadline_budget_without_interpreting_uart(
    service_factory, outcome
):
    backend = MockBoardBackend(MockBehavior(hang=outcome != "success", uart=b""))
    service = service_factory({"board-0": backend})
    await service.start()
    job = await service.submit(
        JobConfig(run_timeout=1, resource_constraints=ResourceConstraints(board="board-0"))
    )
    await until(lambda: service.db.get(job.spec.job_id).state == "RUNNING")
    if outcome == "cancel":
        service.cancel(job.spec.job_id)
    await until(lambda: service.db.get(job.spec.job_id).state.terminal)
    events = service.db.job_events(job.spec.job_id)
    names = [event.type for event in events]
    if outcome == "hang":
        assert names.count("JOB_HANG_WARNING") == 1 and names[-1] == "JOB_TIMED_OUT"
        assert names.index("JOB_HANG_WARNING") < names.index("JOB_TIMED_OUT")
    else:
        assert "JOB_HANG_WARNING" not in names
        assert names[-1] == ("JOB_SUCCEEDED" if outcome == "success" else "JOB_CANCELED")


async def test_real_agent_success_retention_warning_and_deletion_reach_all_provider_consumers(
    scheduler_db, connected_agents, auth_stack, monkeypatch
):
    sessions, _, _, provider, _ = auth_stack
    await sessions.issue(provider.identity)
    requests = []

    def handle(request):
        requests.append(request)
        return success(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        workflow = Notifications(
            scheduler_db, settings(backfill=False), http, "https://scheduler.test"
        )
        spec = await completed(scheduler_db)
        await until(lambda: has_event(scheduler_db, spec.job_id, "JOB_SUCCEEDED"))
        future = utcnow() + timedelta(days=spec.collateral_ttl_days - 1)
        monkeypatch.setattr("fl_scheduler.notifications.alerts.utcnow", lambda: future)
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda _: Alerts(scheduler_db).sweep(), range(4)))
        from fl_scheduler.api.control import Control

        Control(scheduler_db).delete_artifacts(spec.job_id, OWNER)
        await until(lambda: has_event(scheduler_db, spec.job_id, "ARTIFACT_DELETED"))
        for _ in range(8):
            make_due(scheduler_db)
            await workflow.tick()
        with scheduler_db.transaction() as session:
            warnings = list(session.scalars(select(Event).where(Event.type == "ARTIFACT_EXPIRING")))
            assert len(warnings) == 1
            rows = list(session.scalars(select(Notification)))
            assert len(rows) == 12 and all(row.state == "SENT" for row in rows)
            assert {row.payload["event_type"] for row in rows} == {
                "JOB_SUCCEEDED",
                "ARTIFACT_EXPIRING",
                "ARTIFACT_DELETED",
            }
        assert len(requests) == 12
        assert "untrusted secret" not in str([request.content for request in requests])


def has_event(db, job_id, name):
    with db.transaction() as session:
        return (
            session.scalar(
                select(Event.event_id).where(Event.job_id == job_id, Event.type == name).limit(1)
            )
            is not None
        )


def test_concurrent_downtime_sweeps_emit_one_event_without_reassigning_or_ending_jobs(
    scheduler_db, config
):
    from fl_scheduler.db.models import Cluster, Job
    from fl_scheduler.scheduler.placement import Placement

    from tests.integration.test_public_api import registered_ready

    inventory, owner = registered_ready(scheduler_db, config)
    spec = Placement(scheduler_db).submit(JobConfig(), owner)
    Placement(scheduler_db).reserve_pending()
    with scheduler_db.transaction() as session:
        session.get(Cluster, inventory.cluster_id).last_heartbeat = utcnow() - timedelta(minutes=10)
        session.get(Job, spec.job_id).state = "RUNNING"
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda _: Reconciler(scheduler_db).health_sweep(), range(4)))
    with scheduler_db.transaction() as session:
        assert len(list(session.scalars(select(Event).where(Event.type == "CLUSTER_OFFLINE")))) == 1
        assert session.get(Job, spec.job_id).state == "RUNNING"


def test_retention_warning_scan_advances_past_already_warned_jobs_in_bounded_batches(scheduler_db):
    from fl_common.models import ArtifactRecord, ArtifactRef, JobSpec
    from fl_scheduler.db.models import Artifact, Job

    with scheduler_db.transaction() as session:
        for _ in range(105):
            spec = JobSpec.from_config(JobConfig(), OWNER.email)
            session.add(
                Job(
                    job_id=spec.job_id,
                    owner=OWNER.email,
                    spec=spec.model_dump(mode="json"),
                    state="SUCCEEDED",
                    priority=0,
                    submitted_at=spec.submitted_at,
                )
            )
            session.flush()
            record = ArtifactRecord(
                job_id=spec.job_id,
                expires_at=utcnow() + timedelta(hours=1),
                ref=ArtifactRef(kind="results", sha256="0" * 64, size_bytes=0),
            )
            session.add(
                Artifact(
                    job_id=spec.job_id, kind="results", metadata_json=record.model_dump(mode="json")
                )
            )
    Alerts(scheduler_db).sweep()
    with scheduler_db.transaction() as session:
        assert (
            len(list(session.scalars(select(Event).where(Event.type == "ARTIFACT_EXPIRING"))))
            == 100
        )
    Alerts(scheduler_db).sweep()
    Alerts(scheduler_db).sweep()
    with scheduler_db.transaction() as session:
        assert (
            len(list(session.scalars(select(Event).where(Event.type == "ARTIFACT_EXPIRING"))))
            == 105
        )
