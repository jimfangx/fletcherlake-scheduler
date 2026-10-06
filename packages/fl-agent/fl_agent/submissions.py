"""Durable local staging and trusted scheduler submissions, independent of lifecycle."""

import asyncio
import getpass
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import (
    ArtifactRef,
    BoardConfig,
    ClusterConfig,
    JobConfig,
    JobRecord,
    JobSpec,
    JobState,
)
from fl_common.models.artifact import ArtifactKind
from fl_common.scheduling import QueuePolicy, matches

from .board_worker import BoardWorker
from .collateral import CollateralStore
from .db import AgentDB


class SubmissionManager:
    def __init__(
        self,
        config: ClusterConfig,
        db: AgentDB,
        store: CollateralStore,
        workers: dict[str, BoardWorker],
        is_ready: Callable[[], bool],
    ) -> None:
        self.config = config
        self.db = db
        self.store = store
        self.workers = workers
        self.is_ready = is_ready
        self.tasks: set[asyncio.Task[Any]] = set()

    def _choose_board(self, config: JobConfig) -> str:
        boards = [
            board
            for board in self.config.boards
            if board is not None and matches(board, config.resource_constraints)
        ]
        health = {board["board_id"]: board["state"] for board in self.db.boards()}
        boards = [board for board in boards if health.get(board.board_id) != "ERROR"]
        if not boards:
            raise PlatformError(
                "NO_MATCHING_BOARD", "No healthy board satisfies the resource constraints"
            )
        jobs = self.db.jobs()
        policy = QueuePolicy()

        def score(board: BoardConfig) -> tuple[float, str]:
            queued = [
                job.spec.run_timeout_seconds
                for job in jobs
                if job.board_id == board.board_id and job.state == "QUEUED"
            ]
            running = next(
                (
                    job.spec.run_timeout_seconds
                    for job in jobs
                    if job.board_id == board.board_id
                    and not job.state.terminal
                    and job.state != "QUEUED"
                ),
                0,
            )
            return policy.score(queued, running), board.board_id

        return min(boards, key=score).board_id

    async def submit(self, config: JobConfig) -> JobRecord:
        if not self.is_ready():
            raise PlatformError(
                "CLUSTER_NOT_READY", "Cluster configuration is incomplete or draining"
            )
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        job_id = uuid.uuid4()
        spec = JobSpec.from_config(config, getpass.getuser(), job_id=job_id)
        try:
            board_id = self._choose_board(config)
            self.db.create(spec, board_id)
            self.store.save_spec(spec)
            self.db.transition(job_id, JobState.STAGING)
            # Copy before constructing references; the canonical spec hashes the copied bytes.
            binary = (
                await self._copy_input(job_id, "binary", Path(config.binary))
                if config.binary
                else None
            )
            bitstream = (
                await self._copy_input(job_id, "bitstream", Path(config.bitstream))
                if config.bitstream
                else None
            )
            spec.binary = binary
            spec.bitstream = bitstream
            if not self.is_ready():
                raise PlatformError(
                    "CLUSTER_NOT_READY", "Cluster stopped accepting jobs during staging"
                )
            self.db.attach_inputs(spec)
            self.store.save_spec(spec)
            record = self.db.enqueue(job_id)
            self.workers[board_id].wake()
            return record
        except asyncio.CancelledError:
            self.db.cancel(job_id)
            raise
        except Exception as error:
            if any(job.spec.job_id == job_id for job in self.db.jobs()):
                current = self.db.get(job_id)
                if not current.state.terminal:
                    self.db.transition(job_id, JobState.FAILED, str(error))
            raise
        finally:
            self.tasks.discard(task)

    async def _copy_input(self, job_id: UUID, kind: ArtifactKind, source: Path) -> ArtifactRef:
        # Python cannot cancel an active thread. Reap the copy before closing DB/storage.
        copying = asyncio.create_task(
            asyncio.to_thread(self.store.copy_input, job_id, kind, source)
        )
        try:
            return await asyncio.shield(copying)
        except asyncio.CancelledError:
            await copying
            raise

    def stage(self, spec: JobSpec, board_id: str) -> JobRecord:
        """Reserve the durable local staging directory before the gateway sends bytes."""
        if not self.is_ready():
            raise PlatformError("CLUSTER_NOT_READY", "Cluster is not accepting new jobs")
        board = next(
            (b for b in self.config.boards if b is not None and b.board_id == board_id), None
        )
        if board is None or not matches(board, spec.resource_constraints):
            raise PlatformError("NO_MATCHING_BOARD", "Assigned board is incompatible")
        current = self.db.create(spec, board_id)
        if current.state == JobState.CREATED:
            self.store.save_spec(spec)
            return self.db.transition(spec.job_id, JobState.STAGING)
        return current

    async def enqueue(self, spec: JobSpec, board_id: str) -> JobRecord:
        """Trusted scheduler entry point after transport verifies all artifact bytes."""
        current = self.stage(spec, board_id)
        if current.state != JobState.STAGING:
            if current.state == JobState.QUEUED:
                self.workers[board_id].wake()
            return current
        try:
            await asyncio.to_thread(self.store.verify, spec)
            if self.db.get(spec.job_id).state.terminal:
                return self.db.get(spec.job_id)
            self.store.save_spec(spec)
            record = self.db.enqueue(spec.job_id)
            self.workers[board_id].wake()
            return record
        except Exception as error:
            if not self.db.get(spec.job_id).state.terminal:
                self.db.transition(spec.job_id, JobState.FAILED, str(error))
            raise
