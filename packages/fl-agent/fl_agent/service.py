"""Agent lifecycle and API operations; the CLI never instantiates this service."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import psutil
from fl_common.errors import PlatformError
from fl_common.models import (
    BoardConfig,
    ClusterConfig,
    ClusterState,
    JobConfig,
    JobRecord,
    JobSpec,
)
from fl_common.models.base import utcnow

from .board_worker import BoardWorker
from .cache import BitstreamCache
from .collateral import CollateralStore
from .configuration import config_confirmed
from .db import AgentDB
from .exports import AgentExports
from .files import atomic_write
from .hardware.base import BoardBackend
from .hardware.lilikoi import LilikoiBoardBackend
from .hardware.mock import MockBoardBackend
from .locks import ExclusiveLock
from .logs import AgentLogs
from .recovery import recover_jobs
from .submissions import SubmissionManager
from .transfers import AgentTransfers

logger = logging.getLogger(__name__)


class AgentLink(Protocol):
    connected: bool

    def start(self) -> None: ...
    async def stop(self) -> None: ...


class AgentService:
    def __init__(
        self,
        config: ClusterConfig,
        state_root: Path,
        runtime_root: Path,
        backends: dict[str, BoardBackend] | None = None,
    ) -> None:
        self.config = config
        self.state_root = state_root
        self.runtime_root = runtime_root
        self.state = ClusterState.CONFIGURATION_INCOMPLETE
        self.lock = ExclusiveLock(state_root / "agent.lock")
        self.runtime_lock = ExclusiveLock(runtime_root / "agent.lock")
        self.db: AgentDB
        self.store: CollateralStore
        self.cache: BitstreamCache
        self.workers: dict[str, BoardWorker] = {}
        self._backends = backends or {}
        self._sweeper: asyncio.Task[None] | None = None
        self._accepting = False
        self.scheduler_link: AgentLink | None = None

    async def start(self) -> None:
        self.lock.acquire()
        try:
            self.runtime_lock.acquire()
            self.db = AgentDB(self.state_root / "agent.db")
            self.store = CollateralStore(self.state_root / "jobs", self.db)
            self.logs = AgentLogs(self.db, self.store)
            self.cache = BitstreamCache(self.runtime_root)
            self.cache.clear()
            boards = [board for board in self.config.boards if board is not None]
            self.db.initialize_boards([board.board_id for board in boards])
            recover_jobs(self.db)
            if config_confirmed(self.state_root):
                previous = self.db.metadata("cluster_state")
                self.state = (
                    ClusterState(previous)
                    if previous
                    in {
                        ClusterState.DRAINING,
                        ClusterState.DESTROYED,
                        ClusterState.POWERED_OFF,
                        ClusterState.FORCE_POWER_OFF_PENDING,
                    }
                    else ClusterState.READY
                )
            self.db.set_metadata("cluster_state", self.state)
            # Removing a board with queued work requires an explicit operator decision.
            if any(
                not job.state.terminal and job.board_id not in {b.board_id for b in boards}
                for job in self.db.jobs()
            ):
                self.state = ClusterState.CONFIGURATION_INCOMPLETE
                self.db.set_metadata("cluster_state", self.state)
            for board in boards:
                backend = self._backends.get(board.board_id) or self._make_backend(board)
                worker = BoardWorker(
                    board.board_id,
                    backend,
                    self.db,
                    self.store,
                    self.cache,
                    self.runtime_root,
                    can_execute=lambda: self._accepting and config_confirmed(self.state_root),
                )
                self.workers[board.board_id] = worker
                await worker.start(enabled=self.state == ClusterState.READY)
            self._accepting = self.state == ClusterState.READY
            self.submissions = SubmissionManager(
                self.config,
                self.db,
                self.store,
                self.workers,
                lambda: self._accepting and config_confirmed(self.state_root),
            )
            self.transfers = AgentTransfers(
                self.db,
                self.store,
                lambda: self._accepting and config_confirmed(self.state_root),
                executable=self.config.environment.rclone.path
                if self.config.environment.rclone
                else None,
            )
            self.exports = AgentExports(self.db, self.store, self.transfers.executable)
            for worker in self.workers.values():
                worker.wake()
            self._repair_retention()
            self.store.sweep()
            self._sweeper = asyncio.create_task(self._sweep_loop())
            if self.scheduler_link:
                self.scheduler_link.start()
        except BaseException:
            await asyncio.gather(
                *(worker.stop() for worker in self.workers.values()), return_exceptions=True
            )
            if hasattr(self, "db"):
                self.db.close()
            self.lock.release()
            self.runtime_lock.release()
            raise

    @staticmethod
    def _make_backend(board: BoardConfig) -> BoardBackend:
        if board.backend == "lilikoi":
            return LilikoiBoardBackend(
                board.firmware_commands,
                board.firmware_timeout_seconds,
                board.device_mapping,
            )
        return MockBoardBackend()

    def _repair_retention(self) -> None:
        for job in self.db.jobs():
            if job.state.terminal and job.finished_at:
                pending = self.db.connection.execute(
                    "SELECT 1 FROM jobs WHERE job_id=? AND retention_finalized=0",
                    (str(job.spec.job_id),),
                ).fetchone()
                if pending:
                    result_path = self.store.path(job.spec.job_id, "results")
                    if not result_path.exists():
                        samples = [
                            event.payload
                            for event in self.db.job_events(
                                job.spec.job_id,
                                "SHMOO_SAMPLE",
                            )
                        ]
                        result = {"passed": False, "state": job.state, "error": job.error}
                        if samples:
                            result["shmoo"] = {"samples": samples}
                        atomic_write(result_path, json.dumps(result, indent=2).encode())
                    self.store.finalize(job.spec, job.finished_at)

    async def _sweep_loop(self) -> None:
        while True:
            try:
                self._repair_retention()
                self.store.sweep()
                self.db.sweep_command_receipts()
            except Exception:
                logger.exception("Collateral sweep failed; durable deletion will be retried")
            await asyncio.sleep(30)

    async def submit(self, config: JobConfig) -> JobRecord:
        return await self.submissions.submit(config)

    def stage(self, spec: JobSpec, board_id: str) -> JobRecord:
        return self.submissions.stage(spec, board_id)

    async def enqueue(self, spec: JobSpec, board_id: str) -> JobRecord:
        return await self.submissions.enqueue(spec, board_id)

    def cancel(self, job_id: UUID) -> JobRecord:
        record = self.db.cancel(job_id)
        self.transfers.cancel(job_id)
        if record.board_id in self.workers:
            self.workers[record.board_id].cancel(job_id)
        return record

    async def drain(self, state: ClusterState = ClusterState.DRAINING) -> None:
        self._accepting = False
        self.state = state
        self.db.set_metadata("cluster_state", state)
        await self.transfers.stop()
        # Let acknowledged input copies finish before tearing down their DB and workers.
        submissions = list(self.submissions.tasks)
        if submissions:
            await asyncio.gather(*submissions, return_exceptions=True)
        stopped = await asyncio.gather(
            *(worker.stop() for worker in self.workers.values()), return_exceptions=True
        )
        if state == ClusterState.DESTROYED:
            # A retired cluster cannot execute its remaining durable queue.
            for job in self.db.jobs():
                if not job.state.terminal:
                    self.cancel(job.spec.job_id)
            self._repair_retention()
        self.db.record_event(f"CLUSTER_{state}")
        self.db.checkpoint()
        if any(isinstance(result, BaseException) for result in stopped) or any(
            board["state"] == "ERROR" for board in self.db.boards()
        ):
            raise PlatformError(
                "SHUTDOWN_FAILED", "Board shutdown failed; reboot was not authorized"
            )

    async def stop(self) -> None:
        self._accepting = False
        await self.exports.stop()
        if self.state == ClusterState.READY:
            self.state = ClusterState.DRAINING
        await self.transfers.stop()
        if self.submissions.tasks:
            await asyncio.gather(*list(self.submissions.tasks), return_exceptions=True)
        if self._sweeper:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
        await asyncio.gather(
            *(worker.stop() for worker in self.workers.values()), return_exceptions=True
        )
        if self.scheduler_link:
            await self.scheduler_link.stop()
        self.db.checkpoint()
        self.db.close()
        self.lock.release()
        self.runtime_lock.release()

    def snapshot(self) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(str(self.state_root))
        jobs = self.db.jobs()
        return {
            "schema_version": 1,
            "cluster": self.config.model_dump(mode="json"),
            "state": self.state,
            "timestamp": utcnow().isoformat(),
            "boards": self.db.boards(),
            "jobs": [job.model_dump(mode="json") for job in jobs],
            "queues": {
                board_id: [
                    str(job.spec.job_id)
                    for job in jobs
                    if job.board_id == board_id and job.state == "QUEUED"
                ]
                for board_id in self.workers
            },
            "artifacts": [record.model_dump(mode="json") for record in self.store.records()],
            "logs": self.logs.heads(),
            "last_event_sequence": self.db.last_sequence(),
            "system": {
                "cpu": psutil.cpu_percent() / 100,
                "memory": memory.percent / 100,
                "disk_free": disk.free,
                "disk_total": disk.total,
                "scheduler_connected": self.scheduler_link.connected
                if self.scheduler_link
                else False,
            },
        }
