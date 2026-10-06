"""Local execution warnings are durable events; provider delivery belongs to the scheduler."""

import asyncio

from fl_common.models import JobSpec, JobState

from .db import AgentDB


async def warn_execution(db: AgentDB, spec: JobSpec) -> None:
    # Silence is normal UART behavior. Warn on consumed deadline budget, not missing text.
    await asyncio.sleep(spec.run_timeout_seconds * 0.9)
    job = db.get(spec.job_id)
    if job.state in {
        JobState.PREPARING,
        JobState.PROGRAMMING_FPGA,
        JobState.PROGRAMMING_SOC,
        JobState.RUNNING,
    }:
        db.record_event(
            "JOB_HANG_WARNING",
            job_id=spec.job_id,
            board_id=job.board_id,
            payload={
                "reason": "execution_deadline_approaching",
                "state": job.state,
                "timeout_seconds": spec.run_timeout_seconds,
            },
        )
