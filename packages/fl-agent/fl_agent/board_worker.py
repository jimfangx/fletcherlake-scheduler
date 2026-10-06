"""One long-lived worker, durable queue, and exclusive OS lock per physical board."""

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models import JobRecord, JobState

from .alerts import warn_execution
from .cache import BitstreamCache
from .collateral import CollateralStore
from .db import AgentDB
from .executor import Executor
from .files import atomic_write
from .hardware.base import BoardBackend
from .locks import ExclusiveLock

logger = logging.getLogger(__name__)


class BoardWorker:
    def __init__(
        self,
        board_id: str,
        backend: BoardBackend,
        db: AgentDB,
        store: CollateralStore,
        cache: BitstreamCache,
        lock_root: Path,
        can_execute: Callable[[], bool] | None = None,
    ) -> None:
        self.board_id = board_id
        self.backend = backend
        self.db = db
        self.store = store
        self.cache = cache
        self.executor = Executor(db, store, cache)
        self.lock = ExclusiveLock(lock_root / f"{board_id}.lock")
        self.active: UUID | None = None
        self._execution: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._powered = False
        self._healthy = True
        self._task: asyncio.Task[None] | None = None
        self.can_execute = can_execute or (lambda: True)
        self._stop_lock = asyncio.Lock()

    async def start(self, enabled: bool) -> None:
        self.lock.acquire()
        try:
            # A previous daemon may have died while the physical SoC was still running.
            await self._shutdown_board()
            self.cache.invalidate(self.board_id)
            self.db.board_ready(self.board_id)
        except Exception as error:
            self._healthy = False
            self.db.board_error(self.board_id, str(error))
            logger.exception("Board recovery failed: %s", self.board_id)
        if enabled and self._healthy:
            self._task = asyncio.create_task(self._loop(), name=f"board:{self.board_id}")

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, job_id: UUID) -> None:
        if self.active == job_id and self._execution and not self._execution.cancelling():
            self._execution.cancel()
        self.wake()

    async def _loop(self) -> None:
        while not self._stopping and self._healthy:
            self._wake.clear()
            job = self.db.claim(self.board_id) if self.can_execute() else None
            if job is None:
                await self._wake.wait()
                continue
            self.active = job.spec.job_id
            self._execution = asyncio.create_task(self._execute(job))
            try:
                await self._execution
            except asyncio.CancelledError:
                # Cancellation can arrive before _execute is first polled.
                if not self.db.get(job.spec.job_id).state.terminal:
                    await self._finish_cancel(job)
            except Exception as error:
                self._healthy = False
                self.db.board_error(self.board_id, str(error))
                if not self.db.get(job.spec.job_id).state.terminal:
                    self.db.transition(job.spec.job_id, JobState.FAILED, str(error))
                    self.db.board_error(self.board_id, str(error))
                logger.exception("Worker persistence failed: %s", self.board_id)
            finally:
                self.active = None
                self._execution = None

    async def _finish_cancel(self, job: JobRecord) -> None:
        state = JobState.INTERRUPTED if self._stopping else JobState.CANCELED
        if state == JobState.CANCELED and self.db.get(job.spec.job_id).state != JobState.CANCELING:
            self.db.cancel(job.spec.job_id)
        await self._cleanup(reset=True)
        if not self._healthy:
            state = JobState.FAILED
        self._finish(job, state, {"passed": False, "error": str(state)})

    async def _execute(self, job: JobRecord) -> None:
        spec = job.spec
        state = JobState.FAILED
        result: dict[str, object] = {"passed": False}
        warning = asyncio.create_task(warn_execution(self.db, spec))
        try:
            async with asyncio.timeout(spec.run_timeout_seconds):
                if not self._powered:
                    await self.backend.power_on()
                    self._powered = True
                output = await self.executor.execute(spec, self.board_id, self.backend)
                result = output
                state = JobState.SUCCEEDED if output.get("passed") else JobState.FAILED
        except asyncio.CancelledError:
            warning.cancel()
            await self._finish_cancel(job)
            return
        except TimeoutError:
            state = JobState.TIMED_OUT
            result["error"] = "Job execution deadline exceeded"
        except Exception as error:
            result["error"] = error.as_dict() if isinstance(error, PlatformError) else str(error)
            logger.exception("Job execution failed: %s", spec.job_id)
        finally:
            warning.cancel()
            await asyncio.gather(warning, return_exceptions=True)
        await self._cleanup(reset=state != JobState.SUCCEEDED or spec.shmoo is not None)
        if not self._healthy:
            state = JobState.FAILED
            result["error"] = "Board cleanup failed; operator recovery required"
        self._finish(job, state, result)

    async def _cleanup(self, *, reset: bool = False) -> None:
        try:
            if reset:
                await self._shutdown_board()
            else:
                async with asyncio.timeout(10):
                    await self.backend.stop()
        except Exception as error:
            self._healthy = False
            self.cache.invalidate(self.board_id)
            self.db.board_error(self.board_id, str(error))
            logger.exception("Board cleanup failed: %s", self.board_id)
            if not reset:
                try:
                    async with asyncio.timeout(10):
                        await self.backend.power_off()
                    self._powered = False
                except Exception:
                    logger.exception("Firmware power-off also failed: %s", self.board_id)

    async def _shutdown_board(self) -> None:
        """Attempt firmware power-off even when the normal stop command fails."""
        errors: list[str] = []
        for name, operation in (("stop", self.backend.stop), ("power_off", self.backend.power_off)):
            try:
                async with asyncio.timeout(10):
                    await operation()
                if name == "power_off":
                    self._powered = False
                    self.cache.invalidate(self.board_id)
            except Exception as error:
                errors.append(f"{name}: {error}")
        if errors:
            raise PlatformError("BOARD_SHUTDOWN_FAILED", "; ".join(errors))

    def _finish(self, job: JobRecord, state: JobState, result: dict[str, object]) -> None:
        payload = json.dumps(result, indent=2).encode()
        atomic_write(self.store.path(job.spec.job_id, "results"), payload)
        if "error" in result:
            atomic_write(self.store.path(job.spec.job_id, "stderr"), payload)
        error = str(result.get("error")) if "error" in result else None
        record = self.db.transition(job.spec.job_id, state, error)
        assert record.finished_at is not None
        self.store.finalize(job.spec, record.finished_at)
        if not self._healthy:
            self.db.board_error(self.board_id, "Board cleanup failed; operator recovery required")

    async def stop(self) -> None:
        async with self._stop_lock:
            await self._stop_owned()

    async def _stop_owned(self) -> None:
        if not self.lock.held:
            return
        self._stopping = True
        self.wake()
        if self._execution and not self._execution.cancelling():
            self._execution.cancel()
        if self._task:
            await self._task
        try:
            await self._shutdown_board()
            self.db.board_ready(self.board_id)
        except Exception as error:
            self.db.board_error(self.board_id, str(error))
            raise
        finally:
            self.cache.invalidate(self.board_id)
            self.lock.release()
