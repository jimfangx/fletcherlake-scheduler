"""A killed retirement or deletion process is recovered by the hardware-free entrypoint."""

import subprocess
import sys
from pathlib import Path

import pytest
from fl_agent.collateral import CollateralStore
from fl_agent.db import AgentDB
from fl_agent.retired_state import retired_root
from fl_common.models.base import utcnow

from tests.retired_history import history


def maintenance(root):
    subprocess.run(
        [sys.executable, "-m", "fl_agent.retention", "--retired-root", str(root)],
        timeout=15,
        check=True,
    )


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_sigkill_around_archive_rename_is_recovered_without_native_services(
    tmp_path, config, boundary
):
    receipt = history(tmp_path / "state", config, archive=False)
    root = retired_root(receipt.state_root)
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("retirement_process.py")),
            str(root),
            boundary,
        ],
        timeout=10,
        check=False,
    )
    assert result.returncode == -9
    assert (root / "pending-destroy.json").is_file()
    assert receipt.state_root.exists() == (boundary == "before")
    maintenance(root)
    assert not (root / "pending-destroy.json").exists()
    assert not receipt.state_root.exists() and receipt.archive(root).is_dir()
    db = AgentDB(receipt.archive(root) / "agent.db")
    try:
        assert db.get(receipt.snapshot.jobs[0].spec.job_id) == receipt.snapshot.jobs[0]
        assert db.metadata("cluster_state") == "DESTROYED"
    finally:
        db.close()


def test_retired_deletion_sigkill_replays_pending_record_and_keeps_history(tmp_path, config):
    receipt = history(tmp_path / "state", config)
    root, archive = (
        retired_root(receipt.state_root),
        receipt.archive(retired_root(receipt.state_root)),
    )
    job = receipt.snapshot.jobs[0]
    db = AgentDB(archive / "agent.db")
    try:
        CollateralStore(archive / "jobs", db).request_deletion(job.spec.job_id)
    finally:
        db.close()
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("deletion_process.py")), str(archive)],
        timeout=10,
        check=False,
    )
    assert result.returncode == -9
    assert not (archive / "jobs" / str(job.spec.job_id)).exists()
    maintenance(root)
    maintenance(root)
    db = AgentDB(archive / "agent.db")
    try:
        assert db.get(job.spec.job_id) == job
        assert [event.type for event in db.events()].count("ARTIFACT_DELETED") == 1
        assert all(
            record.deleted_at <= utcnow()
            for record in CollateralStore(archive / "jobs", db).records()
        )
        assert (
            db.connection.execute("SELECT state FROM collateral_deletion").fetchone()[0] == "DONE"
        )
    finally:
        db.close()
