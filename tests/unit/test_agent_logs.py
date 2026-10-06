"""Mac log receipt replay cannot disclose deleted or expired bytes."""

import asyncio
from datetime import timedelta

import pytest
from fl_agent.commands import CommandHandler
from fl_common.models import JobConfig
from fl_common.models.base import utcnow
from fl_common.protocol import Message, MessageType
from fl_common.protocol.logs import LogRead

from tests.connected import until


@pytest.mark.parametrize("reason", ["delete", "deadline", "replace"])
async def test_log_receipt_replay_rechecks_source_and_expires_without_losing_cancel_receipts(
    service_factory, reason, monkeypatch, tmp_path
):
    service = service_factory()
    await service.start()
    job = await service.submit(JobConfig())
    await until(lambda: service.db.get(job.spec.job_id).state.terminal)
    head = service.logs.head(job.spec.job_id, "stdout")
    request = LogRead(head=head, offset=0, expires_at=utcnow() + timedelta(seconds=30))
    message = Message(type=MessageType.LOG_READ, payload=request.model_dump(mode="json"))
    handler = CommandHandler(service)
    original = await handler.handle(message)
    assert original.accepted and original.result["next_offset"] == head.size_bytes
    assert await asyncio.gather(*(handler.handle(message) for _ in range(10))) == [original] * 10
    cancel = Message(type=MessageType.JOB_CANCEL, payload={"job_id": str(job.spec.job_id)})
    canceled = await handler.handle(cancel)
    assert canceled.accepted
    if reason == "delete":
        service.store.request_deletion(job.spec.job_id)
    elif reason == "deadline":
        monkeypatch.setattr(
            "fl_agent.logs.utcnow", lambda: request.expires_at + timedelta(seconds=1)
        )
    else:
        outside = tmp_path / "other-file"
        outside.write_bytes(b"unrelated file")
        path = service.store.path(job.spec.job_id, "stdout")
        path.unlink()
        path.symlink_to(outside)
    replay = await handler.handle(message)
    assert not replay.accepted and not replay.result
    assert replay.error_code == (
        "LOG_REQUEST_EXPIRED" if reason == "deadline" else "ARTIFACT_EXPIRED"
    )
    service.db.sweep_command_receipts(request.expires_at + timedelta(seconds=1))
    rows = service.db.connection.execute("SELECT message_id FROM processed_commands").fetchall()
    assert [row[0] for row in rows] == [str(cancel.message_id)]
    assert await handler.handle(cancel) == canceled
