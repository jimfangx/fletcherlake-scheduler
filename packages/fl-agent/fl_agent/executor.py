"""Execute one claimed job. Board ownership and terminal persistence live in the worker."""

import asyncio
import os
from typing import Any

from fl_common.async_calls import background_call
from fl_common.models import JobSpec, JobState

from .cache import BitstreamCache
from .collateral import CollateralStore
from .db import AgentDB
from .hardware.base import BoardBackend, RunResult
from .shmoo import DiscreteBinarySearch, ShmooSample, ShmooStrategy


class Executor:
    def __init__(
        self,
        db: AgentDB,
        store: CollateralStore,
        cache: BitstreamCache,
        strategy: ShmooStrategy | None = None,
    ) -> None:
        self.db = db
        self.store = store
        self.cache = cache
        self.strategy = strategy or DiscreteBinarySearch()

    async def execute(self, spec: JobSpec, board_id: str, backend: BoardBackend) -> dict[str, Any]:
        await asyncio.to_thread(self.store.verify, spec)
        if spec.bitstream:
            if spec.force_reflash or not self.cache.matches(board_id, spec.bitstream.sha256):
                self.db.transition(spec.job_id, JobState.PROGRAMMING_FPGA)
                self.cache.invalidate(board_id)
                await backend.program_fpga(self.store.path(spec.job_id, "bitstream"))
                self.cache.record(board_id, spec.bitstream.sha256)
        if spec.binary:
            self.db.transition(spec.job_id, JobState.PROGRAMMING_SOC)
            await backend.program_soc(self.store.path(spec.job_id, "binary"))
        self.db.transition(spec.job_id, JobState.RUNNING)
        if spec.shmoo:

            async def trial() -> bool:
                result = await self._run(spec, backend)
                await backend.stop()
                return result.passed

            def record_sample(sample: ShmooSample) -> None:
                self.db.record_event(
                    "SHMOO_SAMPLE",
                    job_id=spec.job_id,
                    board_id=board_id,
                    payload=sample.model_dump(mode="json"),
                )

            result = await self.strategy.execute(spec.shmoo, backend, trial, record_sample)
            return {"passed": True, "shmoo": result.model_dump(mode="json")}
        completion = await self._run(spec, backend)
        return completion.model_dump(mode="json")

    async def _run(self, spec: JobSpec, backend: BoardBackend) -> RunResult:
        await backend.start()
        completion = asyncio.create_task(backend.wait_for_completion())
        uart = asyncio.create_task(self._capture_uart(spec, backend))
        try:
            # An error reading UART must fail the job instead of silently losing its output.
            done, _ = await asyncio.wait({completion, uart}, return_when=asyncio.FIRST_COMPLETED)
            if uart in done:
                await uart
            return await completion
        finally:
            completion.cancel()
            uart.cancel()
            await asyncio.gather(completion, uart, return_exceptions=True)

    async def _capture_uart(self, spec: JobSpec, backend: BoardBackend) -> None:
        path = self.store.path(spec.job_id, "stdout")
        with path.open("ab", buffering=0) as output:
            try:
                while True:
                    chunk = await backend.read_uart()
                    if chunk:
                        offset = output.tell()
                        output.write(chunk)
                        await background_call(os.fsync, output.fileno())
                        self.db.record_event(
                            "JOB_LOG",
                            job_id=spec.job_id,
                            board_id=self.db.get(spec.job_id).board_id,
                            payload={
                                "stream": "stdout",
                                "offset": offset,
                                "next_offset": output.tell(),
                            },
                        )
                    await asyncio.sleep(0.02)
            finally:
                os.fsync(output.fileno())
