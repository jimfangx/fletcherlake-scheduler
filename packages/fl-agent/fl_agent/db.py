"""SQLite WAL transactions are the queue authority, not worker task state.

All methods run on the owning event-loop thread and contain no awaits. Transactions are
short; file IO and firmware execution happen outside them. BEGIN IMMEDIATE serializes
queue claims with cancellation. FULL synchronous plus WAL makes acknowledged state durable.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import JobEvent, JobRecord, JobSpec, JobState
from fl_common.models.base import utcnow
from fl_common.models.events import ACTIVE_STATES

from .state import validate_transition


class AgentDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = FULL")
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            self._migrate()
        except BaseException:
            self.connection.close()
            raise

    def _migrate(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 4:
            raise PlatformError("DATABASE_VERSION", f"Unsupported agent DB version {version}")
        if version == 0:
            self.connection.executescript(Path(__file__).with_name("schema.sql").read_text())
            version = 1
        if version == 1:
            with self.transaction() as connection:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN retention_finalized INTEGER NOT NULL DEFAULT 0",
                )
                connection.execute("PRAGMA user_version = 2")
            version = 2
        if version == 2:
            with self.transaction() as connection:
                connection.execute("CREATE INDEX job_events_job ON job_events(job_id,seq)")
                connection.execute("PRAGMA user_version = 3")
            version = 3
        if version == 3:
            with self.transaction() as connection:
                connection.execute("ALTER TABLE processed_commands ADD COLUMN expires_at TEXT")
                connection.execute("CREATE INDEX command_expiry ON processed_commands(expires_at)")
                connection.execute("PRAGMA user_version = 4")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def checkpoint(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(FULL)")

    def sweep_command_receipts(self, now: datetime | None = None) -> None:
        """Only short-lived read receipts expire; effectful command replay stays durable."""
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM processed_commands WHERE expires_at <= ?",
                ((now or utcnow()).isoformat(),),
            )

    def metadata(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_metadata(self, key: str, value: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO metadata VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _event(
        self,
        connection: sqlite3.Connection,
        job_id: str | None,
        event: str,
        board_id: str | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO job_events(job_id,type,timestamp,board_id,payload) VALUES (?,?,?,?,?)",
            (job_id, event, utcnow().isoformat(), board_id, json.dumps(payload or {})),
        )

    def record_event(
        self,
        event: str,
        *,
        job_id: UUID | None = None,
        board_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as connection:
            self._event(connection, str(job_id) if job_id else None, event, board_id, payload)

    def create(self, spec: JobSpec, board_id: str) -> JobRecord:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT spec,board_id FROM jobs WHERE job_id=?",
                (str(spec.job_id),),
            ).fetchone()
            if existing:
                if (
                    JobSpec.model_validate_json(existing["spec"]) != spec
                    or existing["board_id"] != board_id
                ):
                    raise PlatformError(
                        "JOB_ID_CONFLICT", "Job UUID was used with different content"
                    )
            else:
                now = utcnow().isoformat()
                connection.execute(
                    "INSERT INTO jobs(job_id,spec,board_id,state,priority,submitted_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        str(spec.job_id),
                        spec.model_dump_json(),
                        board_id,
                        JobState.CREATED,
                        spec.priority,
                        spec.submitted_at.isoformat(),
                        now,
                    ),
                )
                self._event(connection, str(spec.job_id), "JOB_CREATED", board_id)
        return self.get(spec.job_id)

    def get(self, job_id: UUID) -> JobRecord:
        row = self.connection.execute(
            "SELECT * FROM jobs WHERE job_id=?", (str(job_id),)
        ).fetchone()
        if row is None:
            raise PlatformError("JOB_NOT_FOUND", f"Unknown job {job_id}")
        return self._record(row)

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            spec=JobSpec.model_validate_json(row["spec"]),
            board_id=row["board_id"],
            state=row["state"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
        )

    def jobs(self) -> list[JobRecord]:
        return [
            self._record(row)
            for row in self.connection.execute(
                "SELECT * FROM jobs ORDER BY priority,submitted_at,job_id",
            )
        ]

    def _transition(
        self,
        connection: sqlite3.Connection,
        job_id: UUID,
        state: JobState,
        error: str | None,
    ) -> None:
        row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (str(job_id),)).fetchone()
        if row is None:
            raise PlatformError("JOB_NOT_FOUND", f"Unknown job {job_id}")
        validate_transition(JobState(row["state"]), state)
        now = utcnow().isoformat()
        started = now if state == JobState.RUNNING else row["started_at"]
        finished = now if state.terminal else None
        connection.execute(
            "UPDATE jobs SET state=?,updated_at=?,started_at=?,finished_at=?,error=? "
            "WHERE job_id=?",
            (state, now, started, finished, error, str(job_id)),
        )
        if state != JobState.QUEUED:
            connection.execute("DELETE FROM board_queue WHERE job_id=?", (str(job_id),))
        if state in ACTIVE_STATES and (
            JobState(row["state"]) in ACTIVE_STATES or state == JobState.PREPARING
        ):
            connection.execute(
                "UPDATE board_state SET state=?,active_job=? WHERE board_id=?",
                (state, str(job_id), row["board_id"]),
            )
        elif state.terminal:
            connection.execute(
                "UPDATE board_state SET state='IDLE',active_job=NULL WHERE active_job=?",
                (str(job_id),),
            )
            spec = JobSpec.model_validate_json(row["spec"])
            expires = (utcnow() + timedelta(days=spec.collateral_ttl_days)).isoformat()
            connection.execute(
                "UPDATE artifacts SET expires_at=? WHERE job_id=? AND deleted_at IS NULL",
                (expires, str(job_id)),
            )
        self._event(connection, str(job_id), f"JOB_{state}", row["board_id"], {"error": error})

    def transition(self, job_id: UUID, state: JobState, error: str | None = None) -> JobRecord:
        with self.transaction() as connection:
            self._transition(connection, job_id, state, error)
        return self.get(job_id)

    def enqueue(self, job_id: UUID) -> JobRecord:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state,board_id FROM jobs WHERE job_id=?", (str(job_id),)
            ).fetchone()
            if row is None:
                raise PlatformError("JOB_NOT_FOUND", f"Unknown job {job_id}")
            if row["state"] != JobState.QUEUED:
                self._transition(connection, job_id, JobState.QUEUED, None)
                connection.execute(
                    "INSERT INTO board_queue VALUES (?,?)", (str(job_id), row["board_id"])
                )
        return self.get(job_id)

    def attach_inputs(self, spec: JobSpec) -> None:
        """Only content references may change while local collateral is being staged."""
        with self.transaction() as connection:
            previous = self.get(spec.job_id)
            if previous.state != JobState.STAGING:
                raise PlatformError(
                    "INVALID_TRANSITION", "Inputs can only be attached during staging"
                )
            if previous.spec.model_dump(exclude={"binary", "bitstream"}) != spec.model_dump(
                exclude={"binary", "bitstream"},
            ):
                raise PlatformError("JOB_ID_CONFLICT", "Staging cannot change trusted job metadata")
            connection.execute(
                "UPDATE jobs SET spec=? WHERE job_id=?",
                (spec.model_dump_json(), str(spec.job_id)),
            )

    def claim(self, board_id: str) -> JobRecord | None:
        job_id: UUID | None = None
        with self.transaction() as connection:
            board = connection.execute(
                "SELECT active_job,state FROM board_state WHERE board_id=?",
                (board_id,),
            ).fetchone()
            if board is None or board["active_job"] is not None or board["state"] == "ERROR":
                return None
            row = connection.execute(
                "SELECT jobs.job_id FROM board_queue JOIN jobs USING(job_id) "
                "WHERE board_queue.board_id=? AND jobs.state='QUEUED' "
                "ORDER BY priority,submitted_at,jobs.job_id LIMIT 1",
                (board_id,),
            ).fetchone()
            if row:
                job_id = UUID(row[0])
                self._transition(connection, job_id, JobState.PREPARING, None)
        return self.get(job_id) if job_id else None

    def cancel(self, job_id: UUID) -> JobRecord:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM jobs WHERE job_id=?", (str(job_id),)
            ).fetchone()
            if row is None:
                raise PlatformError("JOB_NOT_FOUND", f"Unknown job {job_id}")
            old = JobState(row[0])
            if not old.terminal and old != JobState.CANCELING:
                self._transition(connection, job_id, JobState.CANCELING, None)
                if old not in ACTIVE_STATES:
                    self._transition(connection, job_id, JobState.CANCELED, None)
        return self.get(job_id)

    def initialize_boards(self, ids: list[str]) -> None:
        with self.transaction() as connection:
            for board_id in ids:
                connection.execute(
                    "INSERT INTO board_state(board_id,state) VALUES (?,'IDLE') "
                    "ON CONFLICT(board_id) DO NOTHING",
                    (board_id,),
                )

    def board_error(self, board_id: str, error: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE board_state SET state='ERROR',error=? WHERE board_id=?",
                (error, board_id),
            )
            self._event(connection, None, "BOARD_ERROR", board_id, {"error": error})

    def board_ready(self, board_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE board_state SET state='IDLE',active_job=NULL,error=NULL WHERE board_id=?",
                (board_id,),
            )

    def boards(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute("SELECT * FROM board_state ORDER BY board_id")
        ]

    def events(self, after: int = 0, limit: int = 1000) -> list[JobEvent]:
        return [
            JobEvent(
                seq=row["seq"],
                job_id=row["job_id"],
                type=row["type"],
                timestamp=row["timestamp"],
                board_id=row["board_id"],
                payload=json.loads(row["payload"]),
            )
            for row in self.connection.execute(
                "SELECT * FROM job_events WHERE seq>? ORDER BY seq LIMIT ?",
                (after, limit),
            )
        ]

    def job_events(self, job_id: UUID, event_type: str | None = None) -> list[JobEvent]:
        """Indexed history for one job, independent from network replay pagination."""
        return [
            JobEvent.model_validate({**dict(row), "payload": json.loads(row["payload"])})
            for row in self.connection.execute(
                "SELECT * FROM job_events WHERE job_id=? AND (? IS NULL OR type=?) ORDER BY seq",
                (str(job_id), event_type, event_type),
            )
        ]

    def last_sequence(self) -> int:
        return int(
            self.connection.execute("SELECT COALESCE(MAX(seq),0) FROM job_events").fetchone()[0]
        )
