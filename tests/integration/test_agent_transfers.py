"""Mac staging and SHA verification gate execution independently of gateway validation."""

import asyncio
import io

from fl_agent.commands import CommandHandler
from fl_common.bbcp import BBCP
from fl_common.models import JobConfig, JobSpec
from fl_common.protocol import Message, MessageType
from fl_common.protocol.delivery import FetchCommand, StageReceipt
from fl_gateway.stream import receive

from tests.transfer import TOKEN_HASH, grant


def stage_message(spec, transfer_id):
    return Message(
        type=MessageType.JOB_STAGE,
        payload={
            "spec": spec.model_dump(mode="json"),
            "board_id": "board-0",
            "transfer_id": str(transfer_id),
        },
    )


async def staged(service, spec, transfer_id):
    handler = CommandHandler(service)
    message = stage_message(spec, transfer_id)
    ack = await handler.handle(message)
    assert ack.accepted
    assert await handler.handle(message) == ack
    return handler, StageReceipt.model_validate(ack.result)


async def test_real_gateway_to_agent_to_hardware(bbcp_gateway, service_factory, tmp_path):
    store, endpoint, binary = bbcp_gateway
    data = b"real BBCP agent collateral" * 8192
    upload, _ = grant(tmp_path, data)
    spec = JobSpec.from_config(
        JobConfig(binary="input.elf"), "alice@berkeley.edu", binary=upload.files[0]
    )
    upload.job_id = spec.job_id
    store.register(upload)
    receive(store, upload.transfer_id, "binary", io.BytesIO(data))
    store.verify(upload.transfer_id, TOKEN_HASH)
    service = service_factory()
    await service.start()
    download, _ = grant(tmp_path, data, source=upload)
    handler, receipt = await staged(service, spec, download.transfer_id)
    download.public_key = receipt.public_key
    store.register(download)
    service.transfers.transport = BBCP(str(binary))
    fetch = Message(
        type=MessageType.JOB_FETCH,
        payload=FetchCommand(
            job_id=spec.job_id,
            endpoint=endpoint,
            grant=download,
        ).model_dump(mode="json"),
    )
    ack = await handler.handle(fetch)
    assert ack.accepted, ack.error
    assert await handler.handle(fetch) == ack
    assert service.db.get(spec.job_id).state == "STAGING"
    assert service.store.path(spec.job_id, "binary").read_bytes() == data
    enqueue = Message(
        type=MessageType.JOB_ENQUEUE,
        payload={
            "spec": spec.model_dump(mode="json"),
            "board_id": "board-0",
        },
    )
    assert (await handler.handle(enqueue)).accepted
    async with asyncio.timeout(5):
        while not service.db.get(spec.job_id).state.terminal:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    assert service.db.get(spec.job_id).state == "SUCCEEDED"
    assert (
        sum(event.type == "JOB_INPUTS_VERIFIED" for event in service.db.job_events(spec.job_id))
        == 1
    )


async def test_mac_rejects_bytes_even_when_transport_reports_success(service_factory, tmp_path):
    data = b"expected bytes"
    upload, _ = grant(tmp_path, data)
    spec = JobSpec.from_config(JobConfig(binary="input.elf"), "alice", binary=upload.files[0])
    upload.job_id = spec.job_id
    service = service_factory()
    await service.start()
    download, _ = grant(tmp_path, data, source=upload)
    handler, receipt = await staged(service, spec, download.transfer_id)
    download.public_key = receipt.public_key

    class CorruptTransport:
        async def copy(self, local, *args):
            local.write_bytes(b"wrong bytes")

    service.transfers.transport = CorruptTransport()
    from fl_common.models.transfer import TransferEndpoint

    endpoint = TransferEndpoint(host="gateway.test", host_key=receipt.public_key)
    ack = await handler.handle(
        Message(
            type=MessageType.JOB_FETCH,
            payload=FetchCommand(
                job_id=spec.job_id,
                endpoint=endpoint,
                grant=download,
            ).model_dump(mode="json"),
        )
    )
    assert not ack.accepted and ack.error_code == "ARTIFACT_INTEGRITY"
    assert not service.store.path(spec.job_id, "binary").exists()
    assert not list(service.store.directory(spec.job_id).glob(".fetch-*"))
    assert service.db.get(spec.job_id).state == "STAGING"
    assert not any(event.type == "JOB_RUNNING" for event in service.db.events())


async def test_cancellation_interrupts_payload_without_waiting_for_transfer(
    service_factory, tmp_path
):
    upload, _ = grant(tmp_path, b"expected bytes")
    spec = JobSpec.from_config(JobConfig(binary="input.elf"), "alice", binary=upload.files[0])
    upload.job_id = spec.job_id
    service = service_factory()
    await service.start()
    download, _ = grant(tmp_path, b"expected bytes", source=upload)
    handler, receipt = await staged(service, spec, download.transfer_id)
    download.public_key = receipt.public_key
    entered, stopped = asyncio.Event(), asyncio.Event()

    class SlowTransport:
        async def copy(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    service.transfers.transport = SlowTransport()
    from fl_common.models.transfer import TransferEndpoint

    endpoint = TransferEndpoint(host="gateway.test", host_key=receipt.public_key)
    fetch = Message(
        type=MessageType.JOB_FETCH,
        payload=FetchCommand(
            job_id=spec.job_id,
            endpoint=endpoint,
            grant=download,
        ).model_dump(mode="json"),
    )
    operation = asyncio.create_task(handler.handle(fetch))
    async with asyncio.timeout(2):
        await entered.wait()
        cancel = await handler.handle(
            Message(type=MessageType.JOB_CANCEL, payload={"job_id": str(spec.job_id)})
        )
        assert cancel.accepted
        await stopped.wait()
        ack = await operation
    assert not ack.accepted and ack.error_code == "JOB_CANCELED"
    assert service.db.get(spec.job_id).state == "CANCELED"
    assert not service.store.path(spec.job_id, "binary").exists()
