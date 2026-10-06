"""Delivery windows cannot starve cancellation or cancel before local job creation."""

from fl_common.models import JobConfig
from fl_common.models.base import utcnow
from fl_common.models.scheduler import Principal
from fl_common.protocol import Message, MessageType
from fl_scheduler.agents.commands import Commands
from fl_scheduler.db.models import Command
from fl_scheduler.registry import Registry
from fl_scheduler.scheduler.placement import Placement


def test_bounded_outbox_prioritizes_cancel_only_after_creation_ack(scheduler_db, config):
    cluster = Registry(scheduler_db).register(config, "test-agent")
    owner = Principal(email="alice@example.edu", subject="alice", role="user")
    spec = Placement(scheduler_db).submit(JobConfig(), owner)
    commands = Commands(scheduler_db)
    for _ in range(70):
        commands.issue(cluster.cluster_id, Message(type=MessageType.STATUS_REQUEST))
    creation = commands.issue(
        cluster.cluster_id,
        Message(
            type=MessageType.JOB_STAGE,
            payload={"job_id": str(spec.job_id)},
        ),
        spec.job_id,
    )
    cancellation = commands.issue(
        cluster.cluster_id,
        Message(
            type=MessageType.JOB_CANCEL,
            payload={"job_id": str(spec.job_id)},
        ),
        spec.job_id,
    )
    batch = commands.pending(cluster.cluster_id, limit=32)
    assert len(batch) == 32 and all(item.type == MessageType.STATUS_REQUEST for item in batch)
    assert len(commands.pending(cluster.cluster_id)) == 72
    with scheduler_db.transaction() as session:
        session.get(Command, creation.message_id).acknowledged_at = utcnow()
    batch = commands.pending(cluster.cluster_id, limit=32)
    assert len(batch) == 32 and batch[0].message_id == cancellation.message_id
    assert len({item.message_id for item in batch}) == 32
