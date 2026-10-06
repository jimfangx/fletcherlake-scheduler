"""Ownership and role checks, cancellation races, and secret-free inventory."""

import httpx
import pytest
from fl_common.errors import PlatformError
from fl_common.models import JobConfig, JobRecord, JobState
from fl_common.models.base import utcnow
from fl_common.models.scheduler import ClusterSnapshot, Principal, Role
from fl_scheduler.agents.commands import Commands
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.api.control import Control
from fl_scheduler.auth.google import Identity
from fl_scheduler.db.models import Assignment, Cluster, Command, Job
from fl_scheduler.registry import Registry
from fl_scheduler.scheduler.placement import Placement
from sqlalchemy import select


async def test_api_ownership_roles_and_trusted_submission(auth_stack, scheduler_db, config):
    sessions, _, directory, provider, app = auth_stack
    registry = Registry(scheduler_db)
    inventory = registry.register(config, "private-agent-secret")
    alice = await sessions.issue(provider.identity)
    directory.members["users"].add("bob@example.edu")
    bob = await sessions.issue(Identity("google-bob", "bob@example.edu"))
    directory.members["operators"].add("operator@example.edu")
    operator = await sessions.issue(Identity("google-operator", "operator@example.edu"))
    directory.members["admins"].add("admin@example.edu")
    admin = await sessions.issue(Identity("google-admin", "admin@example.edu"))
    headers = {"Authorization": "Bearer " + alice.access_token.get_secret_value()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://scheduler.test", headers=headers
    ) as client:
        response = await client.post("/api/jobs", json={"config": {"owner": "bob@example.edu"}})
        assert response.status_code == 422
        response = await client.post("/api/jobs", json={"config": {"priority": -5}})
        assert response.status_code == 201
        job_id = response.json()["job_id"]
        assert response.json()["owner"] == provider.identity.email
        assert (await client.get(f"/api/jobs/{job_id}")).status_code == 200
        assert len((await client.get("/api/jobs")).json()) == 1
        response = await client.get("/api/clusters")
        assert "private-agent-secret" not in response.text
        assert "agent_token_hash" not in response.text
        assert "current_session" not in response.text
        cluster_path = f"/api/clusters/{inventory.cluster_id}/drain"
        assert (await client.post(cluster_path)).status_code == 403
        assert (await client.get("/api/admin/users")).status_code == 403
        client.headers["Authorization"] = "Bearer " + bob.access_token.get_secret_value()
        for suffix in ("", "/events", "/artifacts"):
            assert (await client.get(f"/api/jobs/{job_id}{suffix}")).status_code == 403
        assert (await client.post(f"/api/jobs/{job_id}/cancel")).status_code == 403
        assert (await client.delete(f"/api/jobs/{job_id}/artifacts")).status_code == 403
        assert (await client.get("/api/jobs")).json() == []
        client.headers["Authorization"] = "Bearer " + operator.access_token.get_secret_value()
        assert (await client.get(f"/api/jobs/{job_id}")).status_code == 200
        assert (await client.post(f"/api/jobs/{job_id}/cancel")).status_code == 202
        assert (await client.get(f"/api/jobs/{job_id}")).json()["state"] == "CANCELED"
        assert (await client.post(cluster_path)).status_code == 202
        assert (await client.get("/api/admin/users")).status_code == 403
        client.headers["Authorization"] = "Bearer " + admin.access_token.get_secret_value()
        assert len((await client.get("/api/admin/users")).json()) == 4
        client.headers.pop("Authorization")
        assert (await client.get("/api/clusters")).status_code == 401
        assert (await client.get("/healthz")).status_code == 200


def registered_ready(scheduler_db, config):
    inventory = Registry(scheduler_db).register(config, "agent-secret")
    with scheduler_db.transaction() as session:
        cluster = session.get(Cluster, inventory.cluster_id)
        cluster.state, cluster.health, cluster.last_heartbeat = "READY", "ONLINE", utcnow()
    principal = Principal(email="alice@example.edu", subject="alice", role=Role.USER)
    return inventory, principal


def test_cancel_fences_enqueue_and_stale_staging_snapshot(scheduler_db, config):
    inventory, principal = registered_ready(scheduler_db, config)
    placement = Placement(scheduler_db)
    spec = placement.submit(JobConfig(), principal)
    assignment = placement.reserve_pending()[0]
    Control(scheduler_db).cancel(spec.job_id, principal)
    with pytest.raises(PlatformError, match="cancellation prevents enqueue"):
        Commands(scheduler_db).enqueue_after_transfer(spec.job_id)
    reconcile = Reconciler(scheduler_db)
    session_id = reconcile.connected(inventory.cluster_id)
    snapshot = ClusterSnapshot(
        cluster=inventory,
        state="READY",
        timestamp=utcnow(),
        boards=[],
        jobs=[
            JobRecord(
                spec=spec, board_id=assignment.board_id, state=JobState.STAGING, updated_at=utcnow()
            )
        ],
        queues={},
        artifacts=[],
        last_event_sequence=0,
        system={"cpu": 0, "memory": 0, "disk_free": 1, "disk_total": 2},
    )
    reconcile.snapshot(inventory.cluster_id, session_id, snapshot)
    with scheduler_db.transaction() as session:
        assert session.get(Job, spec.job_id).state == "CANCELED"
        assert session.get(Assignment, spec.job_id).state == "FINISHED"
    assert placement.reserve_pending() == []


def test_pending_drain_survives_ready_snapshot_and_blocks_reservations(scheduler_db, config):
    inventory, principal = registered_ready(scheduler_db, config)
    operator = principal.model_copy(update={"role": Role.OPERATOR})
    Control(scheduler_db).drain(inventory.cluster_id, operator)
    reconcile = Reconciler(scheduler_db)
    connection = reconcile.connected(inventory.cluster_id)
    snapshot = ClusterSnapshot(
        cluster=inventory,
        state="READY",
        timestamp=utcnow(),
        boards=[],
        jobs=[],
        queues={},
        artifacts=[],
        last_event_sequence=0,
        system={"cpu": 0, "memory": 0, "disk_free": 1, "disk_total": 2},
    )
    reconcile.snapshot(inventory.cluster_id, connection, snapshot)
    placement = Placement(scheduler_db)
    placement.submit(JobConfig(), principal)
    assert placement.reserve_pending() == []
    snapshot.state = "DRAINING"
    reconcile.snapshot(inventory.cluster_id, connection, snapshot)
    with scheduler_db.transaction() as session:
        cluster = session.get(Cluster, inventory.cluster_id)
        assert cluster.state == "DRAINING" and cluster.desired_state is None


def test_cancel_outbox_durable_order_and_active_artifact_guard(scheduler_db, config):
    _, principal = registered_ready(scheduler_db, config)
    placement = Placement(scheduler_db)
    spec = placement.submit(JobConfig(), principal)
    placement.reserve_pending()
    enqueue = Commands(scheduler_db).enqueue_after_transfer(spec.job_id)
    control = Control(scheduler_db)
    control.cancel(spec.job_id, principal)
    control.cancel(spec.job_id, principal)
    with scheduler_db.transaction() as session:
        commands = session.scalars(select(Command).order_by(Command.created_at)).all()
        assert len(commands) == 2 and commands[0].message_id == enqueue.message_id
        assert commands[1].envelope["type"] == "JOB_CANCEL"
        assert session.get(Job, spec.job_id).cancel_requested is True
    with pytest.raises(PlatformError, match="only be deleted after completion"):
        control.delete_artifacts(spec.job_id, principal)
