"""Enrollment races and membership cleanup survive scheduler process boundaries."""

import asyncio
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fl_agent.configuration import confirm_config, write_config
from fl_agent.hardware.mock import MockBehavior, MockBoardBackend
from fl_common.errors import PlatformError
from fl_common.models import ClusterState, JobConfig, ResourceConstraints
from fl_common.models.base import utcnow
from fl_common.models.enrollment import EnrollmentRegistration
from fl_common.models.scheduler import ClusterSnapshot, Principal, Role
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.db.models import Artifact, Assignment, Board, Cluster, Event, Job
from fl_scheduler.network.cleanup import NetworkCleanup
from fl_scheduler.network.enrollment import EnrollmentService
from fl_scheduler.network.models import Enrollment, NetworkRevocation
from pydantic import SecretStr
from sqlalchemy import select

from tests.network_helpers import MockNetwork

ADMIN = Principal(email="admin@example.edu", subject="google-admin", role=Role.ADMIN)
BOOTSTRAP = "bootstrap-secret-" + "a" * 48
AGENT_TOKEN = "agent-secret-" + "b" * 48


@pytest.fixture
def enrollment(scheduler_db):
    return EnrollmentService(
        scheduler_db,
        MockNetwork(),
        "https://headscale.test",
        SecretStr(Fernet.generate_key().decode()),
        agent_origin="https://agent.scheduler.test",
    )


def request(config, node):
    return EnrollmentRegistration(
        bootstrap_secret=SecretStr(BOOTSTRAP),
        agent_token=SecretStr(AGENT_TOKEN),
        config=config,
        node_key=node.node_key,
    )


async def test_admin_only_issue_bound_one_use_claim_and_encrypted_retry_receipt(
    enrollment, scheduler_db
):
    for role in (Role.USER, Role.OPERATOR):
        with pytest.raises(PlatformError, match="Administrator role"):
            await enrollment.issue(ADMIN.model_copy(update={"role": role}))
    ticket = await enrollment.issue(ADMIN)
    token = ticket.token.get_secret_value()
    grant = await enrollment.claim(token, BOOTSTRAP)
    retry = await enrollment.claim(token, BOOTSTRAP)
    assert retry == grant and enrollment.network.create_calls == 1
    assert grant.cluster_id == ticket.cluster_id
    assert 599 <= (grant.expires_at - utcnow()).total_seconds() <= 600
    with pytest.raises(PlatformError, match="another setup attempt"):
        await enrollment.claim(token, "different-bootstrap-" + "x" * 48)
    with scheduler_db.transaction() as session:
        row = session.get(Enrollment, ticket.enrollment_id)
        values = repr(row.__dict__)
        assert token not in values and BOOTSTRAP not in values
        assert grant.auth_key.get_secret_value() not in values
        assert row.state == "CLAIMED" and row.key_ciphertext


async def test_concurrent_claim_has_one_issuer_and_failed_provider_is_retryable(enrollment):
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    enrollment.network.before_create = pause
    ticket = await enrollment.issue(ADMIN)
    token = ticket.token.get_secret_value()
    first = asyncio.create_task(enrollment.claim(token, BOOTSTRAP))
    await entered.wait()
    with pytest.raises(PlatformError) as error:
        await enrollment.claim(token, BOOTSTRAP)
    assert error.value.code == "ENROLLMENT_PENDING"
    release.set()
    await first
    assert enrollment.network.create_calls == 1
    other = await enrollment.issue(ADMIN)
    enrollment.network.unavailable = True
    with pytest.raises(PlatformError, match="Headscale unavailable"):
        await enrollment.claim(other.token.get_secret_value(), BOOTSTRAP)
    enrollment.network.unavailable = False
    await enrollment.claim(other.token.get_secret_value(), BOOTSTRAP)
    assert enrollment.network.create_calls == 2


async def test_register_verifies_joined_node_and_consumes_ticket_atomically(
    enrollment, config, scheduler_db
):
    ticket = await enrollment.issue(ADMIN)
    token = ticket.token.get_secret_value()
    await enrollment.claim(token, BOOTSTRAP)
    node = enrollment.network.join("1")
    malicious = enrollment.network.join("999")
    with pytest.raises(PlatformError, match="not joined with this enrollment key"):
        await enrollment.register(token, request(config, malicious))
    registered = await enrollment.register(token, request(config, node))
    assert registered.cluster_id == ticket.cluster_id
    assert await enrollment.register(token, request(config, node)) == registered
    with pytest.raises(PlatformError) as error:
        await enrollment.claim(token, BOOTSTRAP)
    assert error.value.code == "ENROLLMENT_REGISTERED"
    changed = request(config, node).model_copy(
        update={"agent_token": SecretStr("different-agent-" + "x" * 48)}
    )
    with pytest.raises(PlatformError, match="receipt does not match"):
        await enrollment.register(token, changed)
    with scheduler_db.transaction() as session:
        row = session.get(Enrollment, ticket.enrollment_id)
        cluster = session.get(Cluster, ticket.cluster_id)
        assert row.state == "REGISTERED" and row.key_ciphertext is None
        assert cluster.headscale_node_id == node.node_id
        assert cluster.headscale_addresses == list(node.addresses)
        assert AGENT_TOKEN not in repr(cluster.__dict__)
        assert (
            len(session.scalars(select(Event).where(Event.type == "CLUSTER_REGISTERED")).all()) == 1
        )


async def test_revoke_during_key_issuance_records_orphan_cleanup(enrollment, scheduler_db):
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    enrollment.network.before_create = pause
    ticket = await enrollment.issue(ADMIN)
    pending = asyncio.create_task(enrollment.claim(ticket.token.get_secret_value(), BOOTSTRAP))
    await entered.wait()
    await enrollment.revoke(ticket.enrollment_id, ADMIN)
    release.set()
    with pytest.raises(PlatformError, match="revoked during key issuance"):
        await pending
    with scheduler_db.transaction() as session:
        row = session.scalar(select(NetworkRevocation))
        assert row.kind == "KEY" and row.object_id == "1" and row.state == "PENDING"


async def test_revoke_and_expiry_cleanup_retry_without_registering_node(
    enrollment, scheduler_db, config
):
    ticket = await enrollment.issue(ADMIN)
    token = ticket.token.get_secret_value()
    await enrollment.claim(token, BOOTSTRAP)
    node = enrollment.network.join("1")
    await enrollment.revoke(ticket.enrollment_id, ADMIN)
    with pytest.raises(PlatformError, match="unavailable or expired"):
        await enrollment.register(token, request(config, node))
    cleanup = NetworkCleanup(enrollment)
    enrollment.network.unavailable = True
    await cleanup.tick()
    with scheduler_db.transaction() as session:
        row = session.scalar(select(NetworkRevocation))
        assert row.state == "PENDING" and row.attempts == 1 and row.error == "NETWORK_UNAVAILABLE"
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        row.finish_after = utcnow() - timedelta(seconds=1)
    enrollment.network.unavailable = False
    await cleanup.tick()
    assert enrollment.network.deleted == ["1"]
    with scheduler_db.transaction() as session:
        assert session.scalar(select(NetworkRevocation)).state == "DONE"
    expired = await enrollment.issue(ADMIN)
    with scheduler_db.transaction() as session:
        session.get(Enrollment, expired.enrollment_id).expires_at = utcnow() - timedelta(seconds=1)
    await cleanup.tick()
    with scheduler_db.transaction() as session:
        assert session.get(Enrollment, expired.enrollment_id).state == "EXPIRED"


async def test_agent_unregister_fences_sessions_and_retries_node_revocation(
    enrollment, scheduler_db, config, service_factory
):
    ticket = await enrollment.issue(ADMIN)
    await enrollment.claim(ticket.token.get_secret_value(), BOOTSTRAP)
    node = enrollment.network.join("1")
    registered = await enrollment.register(ticket.token.get_secret_value(), request(config, node))
    service = service_factory({"board-0": MockBoardBackend(MockBehavior(hang=True))})
    service.config = registered
    write_config(service.state_root, registered)
    confirm_config(service.state_root)
    await service.start()
    parsed = JobConfig(resource_constraints=ResourceConstraints(board="board-0"))
    running = await service.submit(parsed)
    async with asyncio.timeout(5):
        for _ in range(500):
            if service.db.get(running.spec.job_id).state == "RUNNING":
                break
            await asyncio.sleep(0.01)
        assert service.db.get(running.spec.job_id).state == "RUNNING"
    queued = await service.submit(parsed)
    reconciler = Reconciler(scheduler_db)
    session_id = reconciler.connected(ticket.cluster_id)
    initial = ClusterSnapshot.model_validate(service.snapshot())
    reconciler.snapshot(ticket.cluster_id, session_id, initial)
    with pytest.raises(PlatformError, match="credentials are invalid"):
        enrollment.unregister(ticket.cluster_id, "other-agent", initial)
    with pytest.raises(PlatformError, match="Drain the local cluster"):
        enrollment.unregister(ticket.cluster_id, AGENT_TOKEN, initial)
    await service.drain(ClusterState.DESTROYED)
    final = ClusterSnapshot.model_validate(service.snapshot())
    assert all(job.state.terminal for job in final.jobs)
    # A heartbeat can report DESTROYED before the unregister HTTP request arrives.
    reconciler.snapshot(ticket.cluster_id, session_id, final)
    enrollment.unregister(ticket.cluster_id, AGENT_TOKEN, final)
    enrollment.unregister(ticket.cluster_id, AGENT_TOKEN, final)
    with pytest.raises(PlatformError, match="superseded this session"):
        reconciler.snapshot(ticket.cluster_id, session_id, initial)
    with scheduler_db.transaction() as session:
        cluster = session.get(Cluster, ticket.cluster_id)
        assert cluster.state == "DESTROYED" and cluster.current_session is None
        assert cluster.health == "OFFLINE"
        assert len(session.scalars(select(NetworkRevocation)).all()) == 1
        assert session.get(Job, running.spec.job_id).state == "INTERRUPTED"
        assert session.get(Job, queued.spec.job_id).state == "CANCELED"
        assert all(row.state == "FINISHED" for row in session.scalars(select(Assignment)))
        assert all(not row.enabled for row in session.scalars(select(Board)))
        assert all(
            row.metadata_json["expires_at"] is not None for row in session.scalars(select(Artifact))
        )
    await NetworkCleanup(enrollment).tick()
    assert enrollment.network.deleted == [node.node_id]
