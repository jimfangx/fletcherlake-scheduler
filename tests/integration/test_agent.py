import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fl import Cluster
from fl_agent.api import create_app
from fl_agent.configuration import confirm_config, write_config
from fl_agent.hardware.mock import MockBehavior, MockBoardBackend
from fl_agent.service import AgentService
from fl_common.errors import PlatformError
from fl_common.models import JobConfig, JobState, ResourceConstraints
from fl_common.models.base import utcnow


async def wait_for(service: AgentService, job_id, state: JobState | None = None):
    async with asyncio.timeout(10):
        while True:
            record = service.db.get(job_id)
            if (state is None and record.state.terminal) or record.state == state:
                return record
            await asyncio.sleep(0.01)


async def test_three_boards_execute_and_capture_collateral(service_factory, input_files) -> None:
    service = service_factory()
    await service.start()
    jobs = [await service.submit(JobConfig(**input_files)) for _ in range(12)]
    assert {job.board_id for job in jobs} == {"board-0", "board-1", "board-2"}
    results = await asyncio.gather(*(wait_for(service, job.spec.job_id) for job in jobs))
    assert all(job.state == JobState.SUCCEEDED for job in results)
    for job in jobs:
        assert "hello from SoC" in service.store.path(job.spec.job_id, "stdout").read_text()
    assert all(record.expires_at is not None for record in service.store.records())
    assert service.snapshot()["last_event_sequence"] > 12


async def test_sha_cache_skips_repeat_and_force_reflash(service_factory, input_files) -> None:
    board = MockBoardBackend()
    service = service_factory({"board-0": board})
    await service.start()
    config = JobConfig(**input_files, resource_constraints=ResourceConstraints(board="board-0"))
    first = await service.submit(config)
    await wait_for(service, first.spec.job_id)
    second = await service.submit(config)
    await wait_for(service, second.spec.job_id)
    assert board.fpga_program_count == 1
    config.force_reflash = True
    third = await service.submit(config)
    await wait_for(service, third.spec.job_id)
    assert board.fpga_program_count == 2


async def test_running_cancel_preserves_other_queue_and_resets(
    service_factory, input_files
) -> None:
    board = MockBoardBackend(MockBehavior(hang=True))
    service = service_factory({"board-0": board})
    await service.start()
    config = JobConfig(**input_files, resource_constraints=ResourceConstraints(board="board-0"))
    running = await service.submit(config)
    await wait_for(service, running.spec.job_id, JobState.RUNNING)
    queued = await service.submit(config)
    assert service.cancel(queued.spec.job_id).state == JobState.CANCELED
    service.cancel(running.spec.job_id)
    service.cancel(running.spec.job_id)
    assert (await wait_for(service, running.spec.job_id)).state == JobState.CANCELED
    assert not board.running
    assert "power_off" in board.calls


async def test_timeout_and_failure(service_factory, input_files) -> None:
    board = MockBoardBackend(MockBehavior(hang=True))
    service = service_factory({"board-0": board})
    await service.start()
    config = JobConfig(
        **input_files, run_timeout=1, resource_constraints=ResourceConstraints(board="board-0")
    )
    timed_out = await service.submit(config)
    assert (await wait_for(service, timed_out.spec.job_id)).state == JobState.TIMED_OUT
    board.behavior.hang = False
    board.behavior.fail_operation = "program_soc"
    failed = await service.submit(config)
    assert (await wait_for(service, failed.spec.job_id)).state == JobState.FAILED
    assert service.store.path(failed.spec.job_id, "stderr").exists()


async def test_incomplete_configuration_blocks_jobs(service_factory) -> None:
    service = service_factory(confirmed=False)
    await service.start()
    assert service.snapshot()["state"] == "CONFIGURATION_INCOMPLETE"
    with pytest.raises(PlatformError) as error:
        await service.submit(JobConfig())
    assert error.value.code == "CLUSTER_NOT_READY"


async def test_ttl_sweep_is_durable_and_idempotent(service_factory, input_files) -> None:
    service = service_factory()
    await service.start()
    job = await service.submit(JobConfig(**input_files, run_collateral_ttl=1))
    await wait_for(service, job.spec.job_id)
    expires = [
        record.expires_at for record in service.store.records() if record.job_id == job.spec.job_id
    ]
    assert all(expiry > utcnow() for expiry in expires)
    assert service.store.sweep(utcnow() + timedelta(days=2)) == 1
    assert not service.store.directory(job.spec.job_id).exists()
    assert service.store.sweep(utcnow() + timedelta(days=2)) == 0
    assert [event.type for event in service.db.events()].count("ARTIFACT_DELETED") == 1


def test_cli_sdk_calls_same_agent_api(tmp_path: Path, config, input_files) -> None:
    root = tmp_path / "agent"
    write_config(root, config)
    confirm_config(root)
    service = AgentService(config, root, root / "run")
    with TestClient(create_app(service)) as transport:
        cluster = Cluster(transport)
        assert len(cluster.status()["boards"]) == 3
        job = cluster.submit(JobConfig(**input_files))
        assert cluster.job(job.spec.job_id).spec.owner != "spoofed"
        assert cluster.kill(job.spec.job_id).state in {
            JobState.CANCELED,
            JobState.CANCELING,
            JobState.SUCCEEDED,
        }


async def test_reconfiguration_marker_blocks_queued_work_after_restart(
    tmp_path: Path,
    config,
    input_files,
) -> None:
    root = tmp_path / "reconfiguring"
    write_config(root, config)
    confirm_config(root)
    first = AgentService(
        config, root, root / "run", {"board-0": MockBoardBackend(MockBehavior(hang=True))}
    )
    await first.start()
    parsed = JobConfig(**input_files, resource_constraints=ResourceConstraints(board="board-0"))
    active = await first.submit(parsed)
    await wait_for(first, active.spec.job_id, JobState.RUNNING)
    queued = await first.submit(parsed)
    (root / "cluster.sha256").unlink()
    await first.stop()
    second = AgentService(config, root, root / "run")
    await second.start()
    try:
        assert second.state == "CONFIGURATION_INCOMPLETE"
        assert second.db.get(active.spec.job_id).state == JobState.INTERRUPTED
        assert second.db.get(queued.spec.job_id).state == JobState.QUEUED
        assert all(worker.active is None for worker in second.workers.values())
    finally:
        await second.stop()


async def test_failed_board_cleanup_quarantines_board(service_factory, input_files) -> None:
    board = MockBoardBackend()
    service = service_factory({"board-0": board})
    await service.start()
    board.behavior.fail_operation = "stop"
    job = await service.submit(
        JobConfig(**input_files, resource_constraints=ResourceConstraints(board="board-0"))
    )
    assert (await wait_for(service, job.spec.job_id)).state == JobState.FAILED
    assert service.db.boards()[0]["state"] == "ERROR"
    with pytest.raises(PlatformError, match="healthy board"):
        await service.submit(JobConfig(resource_constraints=ResourceConstraints(board="board-0")))


async def test_cancel_before_executor_first_poll_keeps_worker_live(service_factory) -> None:
    service = service_factory()
    await service.start()
    config = JobConfig(resource_constraints=ResourceConstraints(board="board-0"))
    first = await service.submit(config)
    await asyncio.sleep(0)
    service.cancel(first.spec.job_id)
    assert (await wait_for(service, first.spec.job_id)).state == JobState.CANCELED
    second = await service.submit(config)
    assert (await wait_for(service, second.spec.job_id)).state == JobState.SUCCEEDED


async def test_corrupted_collateral_fails_before_hardware_programming(
    service_factory, input_files
) -> None:
    board = MockBoardBackend(MockBehavior(hang=True))
    service = service_factory({"board-0": board})
    await service.start()
    config = JobConfig(**input_files, resource_constraints=ResourceConstraints(board="board-0"))
    first = await service.submit(config)
    await wait_for(service, first.spec.job_id, JobState.RUNNING)
    corrupted = await service.submit(config)
    service.store.path(corrupted.spec.job_id, "binary").write_bytes(b"corrupted")
    service.cancel(first.spec.job_id)
    assert (await wait_for(service, corrupted.spec.job_id)).state == JobState.FAILED
    assert board.soc_program_count == 1
    assert "verification failed" in service.db.get(corrupted.spec.job_id).error


async def test_graceful_restart_preserves_queued_work_and_ttl(
    tmp_path: Path, config, input_files
) -> None:
    from fl_common.models import ClusterState

    root = tmp_path / "restart"
    write_config(root, config)
    confirm_config(root)
    service = AgentService(
        config, root, root / "run", {"board-0": MockBoardBackend(MockBehavior(hang=True))}
    )
    await service.start()
    parsed = JobConfig(**input_files, resource_constraints=ResourceConstraints(board="board-0"))
    active = await service.submit(parsed)
    await wait_for(service, active.spec.job_id, JobState.RUNNING)
    queued = await service.submit(parsed)
    await service.drain(ClusterState.RESTARTING)
    assert service.db.get(active.spec.job_id).state == JobState.INTERRUPTED
    assert service.db.get(queued.spec.job_id).state == JobState.QUEUED
    assert not any((root / "run").glob("*.bitstream.sha256"))
    await service.stop()
    restarted = AgentService(config, root, root / "run")
    await restarted.start()
    try:
        assert restarted.state == ClusterState.READY
        assert (await wait_for(restarted, queued.spec.job_id)).state == JobState.SUCCEEDED
        assert all(record.expires_at is not None for record in restarted.store.records())
    finally:
        await restarted.stop()


async def test_failed_cancel_reports_failure_and_attempts_firmware_power_off(
    service_factory,
    input_files,
) -> None:
    board = MockBoardBackend(MockBehavior(hang=True))
    service = service_factory({"board-0": board})
    await service.start()
    job = await service.submit(
        JobConfig(
            **input_files,
            resource_constraints=ResourceConstraints(board="board-0"),
        )
    )
    await wait_for(service, job.spec.job_id, JobState.RUNNING)
    board.behavior.fail_operation = "stop"
    service.cancel(job.spec.job_id)
    assert (await wait_for(service, job.spec.job_id)).state == JobState.FAILED
    assert not board.powered and not board.running
    assert service.db.boards()[0]["state"] == "ERROR"


async def test_failed_shutdown_blocks_graceful_restart(service_factory) -> None:
    from fl_common.models import ClusterState

    board = MockBoardBackend()
    service = service_factory({"board-0": board})
    await service.start()
    board.behavior.fail_operation = "stop"
    with pytest.raises(PlatformError, match="reboot"):
        await service.drain(ClusterState.RESTARTING)
    assert "power_off" in board.calls
