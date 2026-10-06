from pathlib import Path

import pytest
from fl_agent.db import AgentDB
from fl_agent.recovery import recover_jobs
from fl_common.errors import PlatformError
from fl_common.models import JobConfig, JobSpec, JobState, ResourceConstraints
from fl_common.scheduling import QueuePolicy, matches


def queue(db: AgentDB, priority: int = 0) -> JobSpec:
    db.initialize_boards(["board-0"])
    job = JobSpec.from_config(JobConfig(priority=priority), "alice")
    db.create(job, "board-0")
    db.transition(job.job_id, JobState.STAGING)
    db.enqueue(job.job_id)
    return job


def test_priority_fifo_and_crash_recovery(tmp_path: Path) -> None:
    path = tmp_path / "agent.db"
    db = AgentDB(path)
    db.initialize_boards(["board-0"])
    first, high, last = queue(db), queue(db, -2), queue(db)
    assert db.claim("board-0").spec.job_id == high.job_id
    db.transition(high.job_id, JobState.RUNNING)
    db.close()
    recovered = AgentDB(path)
    recover_jobs(recovered)
    assert recovered.get(high.job_id).state == JobState.INTERRUPTED
    assert recovered.claim("board-0").spec.job_id == first.job_id
    recovered.transition(first.job_id, JobState.FAILED)
    assert recovered.claim("board-0").spec.job_id == last.job_id
    assert recovered.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    recovered.close()


def test_cancel_and_event_transaction(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    spec = queue(db)
    assert db.cancel(spec.job_id).state == JobState.CANCELED
    assert db.claim("board-0") is None
    events = db.events()
    assert [event.type for event in events][-2:] == ["JOB_CANCELING", "JOB_CANCELED"]
    assert [event.seq for event in events] == sorted({event.seq for event in events})
    assert db.cancel(spec.job_id).state == JobState.CANCELED
    db.close()


def test_illegal_transition_rolls_back(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    spec = queue(db)
    sequence = db.last_sequence()
    with pytest.raises(PlatformError, match="Cannot transition"):
        db.transition(spec.job_id, JobState.SUCCEEDED)
    assert db.get(spec.job_id).state == JobState.QUEUED
    assert db.last_sequence() == sequence
    assert db.claim("board-0") is not None
    db.close()


def test_content_conflicts_and_duplicate_enqueue(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    spec = queue(db)
    db.enqueue(spec.job_id)
    assert db.connection.execute("SELECT COUNT(*) FROM board_queue").fetchone()[0] == 1
    changed = spec.model_copy(update={"owner": "mallory"})
    with pytest.raises(PlatformError, match="different content"):
        db.create(changed, "board-0")
    db.close()


def test_queue_score_caps_abusive_timeouts() -> None:
    policy = QueuePolicy()
    assert policy.score([35 * 86400]) == policy.score([2 * 3600])
    assert policy.score([60] * 10) > policy.score([35 * 86400])
    assert policy.score([], 35 * 86400) == policy.score([], 6 * 3600)


def test_resource_matching(config) -> None:
    board = config.boards[0]
    assert matches(board, ResourceConstraints(soc="fletcherlake", fpga="xcvu9p"))
    assert not matches(board, ResourceConstraints(board="board-2"))
    assert not matches(board, ResourceConstraints(soc="unknown"))


def test_only_one_claim_and_queued_cancel_preserves_active_board(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "agent.db")
    running, queued = queue(db), queue(db)
    assert db.claim("board-0").spec.job_id == running.job_id
    assert db.claim("board-0") is None
    db.cancel(queued.job_id)
    assert db.boards()[0]["active_job"] == str(running.job_id)
    assert db.boards()[0]["state"] == "PREPARING"
    db.close()


def test_legacy_database_upgrade_preserves_queued_jobs_and_events(tmp_path: Path) -> None:
    import json
    import sqlite3
    from datetime import timedelta

    import fl_agent.db as agent_db
    from fl_common.models.base import utcnow
    from fl_common.protocol import Ack, Message, MessageType

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(Path(agent_db.__file__).with_name("schema.sql").read_text())
    spec = JobSpec.from_config(JobConfig(), "alice")
    timestamp = utcnow().isoformat()
    legacy.execute(
        "INSERT INTO jobs(job_id,spec,board_id,state,priority,submitted_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (str(spec.job_id), spec.model_dump_json(), "board-0", "QUEUED", 0, timestamp, timestamp),
    )
    legacy.execute("INSERT INTO board_queue VALUES (?,?)", (str(spec.job_id), "board-0"))
    legacy.execute("INSERT INTO board_state(board_id,state) VALUES (?,'IDLE')", ("board-0",))
    legacy.execute(
        "INSERT INTO job_events(job_id,type,timestamp,board_id,payload) VALUES (?,?,?,?,?)",
        (str(spec.job_id), "JOB_QUEUED", timestamp, "board-0", "{}"),
    )
    message = Message(
        type=MessageType.JOB_ENQUEUE,
        payload={"spec": spec.model_dump(mode="json"), "board_id": "board-0"},
    )
    receipt = json.dumps(
        {
            "request": message.model_dump(mode="json"),
            "ack": Ack(
                message_id=message.message_id, accepted=True, result={"state": "QUEUED"}
            ).model_dump(mode="json"),
        }
    )
    legacy.execute(
        "INSERT INTO processed_commands(message_id,response) VALUES (?,?)",
        (str(message.message_id), receipt),
    )
    legacy.commit()
    legacy.close()
    upgraded = AgentDB(path)
    assert upgraded.get(spec.job_id).state == JobState.QUEUED
    assert upgraded.events()[0].type == "JOB_QUEUED"
    upgraded.sweep_command_receipts(utcnow() + timedelta(days=365))
    row = upgraded.connection.execute(
        "SELECT response,expires_at FROM processed_commands"
    ).fetchone()
    assert row[0] == receipt and row[1] is None
    assert upgraded.connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert upgraded.claim("board-0").spec.job_id == spec.job_id
    upgraded.close()
