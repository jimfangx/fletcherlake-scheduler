import subprocess
import sys
from pathlib import Path

from fl_agent.collateral import CollateralStore
from fl_agent.db import AgentDB
from fl_common.models import JobConfig, JobSpec, JobState


def test_deletion_retries_after_files_deleted_before_db_commit(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    store = CollateralStore(tmp_path / "jobs", db)
    spec = JobSpec.from_config(JobConfig(), "alice")
    db.initialize_boards(["board-0"])
    db.create(spec, "board-0")
    store.save_spec(spec)
    db.transition(spec.job_id, JobState.STAGING)
    db.enqueue(spec.job_id)
    db.claim("board-0")
    finished = db.transition(spec.job_id, JobState.FAILED)
    store.finalize(spec, finished.finished_at)
    store.request_deletion(spec.job_id)
    db.close()
    process = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("deletion_process.py")), str(tmp_path)],
        timeout=10,
        check=False,
    )
    assert process.returncode == -9
    recovered = AgentDB(tmp_path / "agent.db")
    recovered_store = CollateralStore(tmp_path / "jobs", recovered)
    assert not recovered_store.directory(spec.job_id).exists()
    pending = recovered.connection.execute("SELECT state FROM collateral_deletion").fetchone()
    assert pending[0] == "PENDING"
    assert recovered_store.sweep() == 1
    assert recovered_store.sweep() == 0
    assert [event.type for event in recovered.events()].count("ARTIFACT_DELETED") == 1
    recovered.close()
