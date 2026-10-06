"""Lost native acknowledgements resume without a daemon and preserve cleanup while pending."""

import asyncio
from datetime import timedelta

import pytest
from fl.macos.provisioning import LABEL
from fl_agent.retired import sweep_retired
from fl_agent.retired_state import retired_root
from fl_agent.retirement import Retirement
from fl_common.errors import PlatformError
from fl_common.files import atomic_write
from fl_common.models import JobConfig, ResourceConstraints

from tests.connected import until


async def test_destroy_resumes_after_daemon_unloaded_without_another_rpc(
    retirement_stack,
    config,
    tmp_path,
):
    stack = retirement_stack
    stack.controls["lose_unregister"] = False
    setup = stack.setup
    ticket = stack.ticket()
    await asyncio.to_thread(
        setup.init,
        config,
        scheduler="https://scheduler.test",
        enrollment_token=ticket["token"],
    )
    await asyncio.to_thread(setup.confirm, tmp_path)
    service = stack.services[-1]
    job = await service.submit(JobConfig(resource_constraints=ResourceConstraints(board="board-1")))
    await until(lambda: service.db.get(job.spec.job_id).state == "SUCCEEDED")
    native = setup.runner

    def lose_ack(argv):
        native(argv)
        if argv[1:3] == ["bootout", f"system/{LABEL}"]:
            raise RuntimeError("Lost native acknowledgement")

    setup.runner = lose_ack
    with pytest.raises(RuntimeError, match="Lost native acknowledgement"):
        await asyncio.to_thread(setup.destroy)
    assert not service.lock.held and not stack.native.loaded(LABEL)
    root = retired_root(setup.state_root)
    receipt = Retirement.load(root)
    assert receipt.phase == "UNREGISTERED"
    assert (setup.state_root / "credentials.json").is_file()
    # The independent job also sweeps a stopped pending retirement before archival.
    expiry = max(record.expires_at for record in receipt.snapshot.artifacts) + timedelta(seconds=1)
    report = sweep_retired(root, now=expiry)
    assert report.failed == 0 and report.deleted == 1
    assert not (setup.state_root / "jobs" / str(job.spec.job_id)).exists()
    for operation in (setup.confirm, setup.reconfigure):
        with pytest.raises(PlatformError) as error:
            await asyncio.to_thread(operation, tmp_path)
        assert error.value.code == "DESTROY_PENDING"
    with pytest.raises(PlatformError) as error:
        await asyncio.to_thread(setup.restart)
    assert error.value.code == "DESTROY_PENDING"
    setup.runner = native
    setup.client_factory = lambda _: pytest.fail("No daemon RPC after native teardown")
    setup.http_factory = lambda: pytest.fail("No unregister replay after durable acknowledgement")
    result = await asyncio.to_thread(setup.destroy)
    assert result == receipt.snapshot
    assert len(stack.unregister_requests) == 1 and stack.calls.count("logout") == 1
    assert not setup.state_root.exists()
    assert await asyncio.to_thread(setup.destroy) == result  # Lost completion is idempotent, too.


async def test_missing_credentials_blocks_teardown_until_unregister_can_be_acknowledged(
    retirement_stack, config, tmp_path
):
    stack = retirement_stack
    stack.controls["lose_unregister"] = False
    setup, ticket = stack.setup, stack.ticket()
    await asyncio.to_thread(
        setup.init,
        config,
        scheduler="https://scheduler.test",
        enrollment_token=ticket["token"],
    )
    await asyncio.to_thread(setup.confirm, tmp_path)
    service = stack.services[-1]
    credentials = setup.state_root / "credentials.json"
    original = credentials.read_bytes()
    credentials.unlink()
    with pytest.raises(PlatformError) as error:
        await asyncio.to_thread(setup.destroy)
    assert error.value.code == "CREDENTIALS_MISSING"
    assert service.lock.held and stack.native.loaded(LABEL)
    assert stack.unregister_requests == [] and "logout" not in stack.calls
    root = retired_root(setup.state_root)
    receipt = Retirement.load(root)
    assert receipt.phase == "DRAINED" and not receipt.archive(root).exists()
    assert (setup.state_root / "agent.db").is_file()
    atomic_write(credentials, original)
    assert await asyncio.to_thread(setup.destroy) == receipt.snapshot
    assert len(stack.unregister_requests) == 1 and not service.lock.held
    assert not setup.state_root.exists() and receipt.archive(root).is_dir()
