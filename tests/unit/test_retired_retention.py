"""Archived cleanup preserves terminal history and isolates active and damaged clusters."""

from datetime import timedelta
from uuid import UUID, uuid4

from fl_agent.collateral import CollateralStore
from fl_agent.configuration import write_config
from fl_agent.db import AgentDB
from fl_agent.retired import sweep_retired
from fl_agent.retired_state import retired_root
from fl_common.locks import ExclusiveLock
from fl_common.models import JobConfig, JobSpec

from tests.retired_history import history


def test_expiry_boundary_preserves_history_and_never_sweeps_new_cluster(tmp_path, config):
    state = tmp_path / "state"
    receipt = history(state, config)
    root, archive = retired_root(state), receipt.archive(retired_root(state))
    expiry = receipt.snapshot.artifacts[0].expires_at
    job = receipt.snapshot.jobs[0]
    assert sweep_retired(root, now=expiry - timedelta(microseconds=1)).deleted == 0
    assert (archive / "jobs" / str(job.spec.job_id)).is_dir()
    replacement = config.model_copy(update={"cluster_id": uuid4()}, deep=True)
    write_config(state, replacement)
    active = AgentDB(state / "agent.db")
    try:
        active.initialize_boards(["board-0"])
        queued = JobSpec.from_config(JobConfig(), "bob")
        active.create(queued, "board-0")
        CollateralStore(state / "jobs", active).save_spec(queued)
        active.set_metadata("cluster_state", "READY")
        with ExclusiveLock(state / "agent.lock"):
            report = sweep_retired(root, now=expiry)
        assert report.deleted == 1 and report.failed == 0
        assert active.get(queued.job_id).state == "CREATED"
        assert (state / "jobs" / str(queued.job_id)).exists()
        assert sweep_retired(root, now=expiry).deleted == 0
    finally:
        active.close()
    old = AgentDB(archive / "agent.db")
    try:
        assert old.get(job.spec.job_id) == job
        assert [event.type for event in old.events()].count("ARTIFACT_DELETED") == 1
        assert all(
            record.deleted_at is not None
            for record in CollateralStore(archive / "jobs", old).records()
        )
    finally:
        old.close()
    assert not (archive / "jobs" / str(job.spec.job_id)).exists()
    assert (archive / "snapshot.json").is_file()


def test_corrupt_archive_does_not_block_other_cluster_and_logs_no_contents(
    tmp_path, config, caplog
):
    state = tmp_path / "state"
    config.cluster_id = UUID(int=1)
    bad = history(state, config)
    config.cluster_id = UUID(int=2)
    good = history(state, config)
    root = retired_root(state)
    (bad.archive(root) / "agent.db").write_bytes(b"private collateral secret in corrupt database")
    report = sweep_retired(root, now=good.snapshot.artifacts[0].expires_at)
    assert report.deleted == 1 and report.failed == 1
    assert "private collateral secret" not in caplog.text
    assert str(bad.snapshot.cluster.cluster_id) in caplog.text
    assert (bad.archive(root) / "jobs" / str(bad.snapshot.jobs[0].spec.job_id)).exists()


def test_cleanup_skips_locked_archive_and_retries_after_lock_release(tmp_path, config):
    receipt = history(tmp_path / "state", config)
    root = retired_root(receipt.state_root)
    with ExclusiveLock(receipt.archive(root) / "agent.lock"):
        report = sweep_retired(root, now=receipt.snapshot.artifacts[0].expires_at)
        assert report.deleted == 0 and report.failed == 0
    assert sweep_retired(root, now=receipt.snapshot.artifacts[0].expires_at).deleted == 1


def test_pending_teardown_retention_runs_without_daemon_or_network(tmp_path, config):
    receipt = history(tmp_path / "state", config, archive=False)
    receipt.phase = "UNREGISTERED"
    root = retired_root(receipt.state_root)
    receipt.save(root)
    report = sweep_retired(root, now=receipt.snapshot.artifacts[0].expires_at)
    assert report.deleted == 1 and report.failed == 0
    assert receipt.state_root.is_dir() and (root / "pending-destroy.json").is_file()
    assert not receipt.archive(root).exists()  # Native teardown still requires an operator retry.


def test_finished_native_teardown_is_archived_by_background_recovery(tmp_path, config):
    receipt = history(tmp_path / "state", config, archive=False)
    root = retired_root(receipt.state_root)
    with ExclusiveLock(root / "management.lock"):
        assert sweep_retired(root).failed == 0
        assert receipt.state_root.exists()
    report = sweep_retired(root, now=receipt.snapshot.artifacts[0].expires_at)
    assert report.deleted == 1 and report.failed == 0
    assert not receipt.state_root.exists() and receipt.archive(root).is_dir()
    assert not (root / "pending-destroy.json").exists()


def test_symlinked_collateral_cannot_delete_outside_archive(tmp_path, config):
    receipt = history(tmp_path / "state", config)
    root = retired_root(receipt.state_root)
    archive = receipt.archive(root)
    jobs = archive / "jobs"
    outside = tmp_path / "outside"
    jobs.rename(outside)
    jobs.symlink_to(outside, target_is_directory=True)
    report = sweep_retired(root, now=receipt.snapshot.artifacts[0].expires_at)
    assert report.failed == 1 and report.deleted == 0
    assert (outside / str(receipt.snapshot.jobs[0].spec.job_id) / "results.json").is_file()


def test_incomplete_retention_cannot_archive_or_delete_pending_history(tmp_path, config):
    receipt = history(tmp_path / "state", config, archive=False)
    root = retired_root(receipt.state_root)
    db = AgentDB(receipt.state_root / "agent.db")
    try:
        with db.transaction() as connection:
            connection.execute("UPDATE jobs SET retention_finalized=0")
    finally:
        db.close()
    report = sweep_retired(root, now=receipt.snapshot.artifacts[0].expires_at)
    assert report.failed == 1 and report.deleted == 0
    assert not receipt.archive(root).exists()
    assert (receipt.state_root / "jobs" / str(receipt.snapshot.jobs[0].spec.job_id)).exists()
    assert (root / "pending-destroy.json").is_file()
