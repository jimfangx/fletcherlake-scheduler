"""Recovery deliberately never resumes hardware execution from durable RUNNING state."""

from fl_common.models import JobState
from fl_common.models.events import ACTIVE_STATES

from .db import AgentDB


def recover_jobs(db: AgentDB) -> None:
    for job in db.jobs():
        if job.state == JobState.CANCELING:
            db.transition(job.spec.job_id, JobState.CANCELED, "Agent restarted during cancellation")
        elif job.state in ACTIVE_STATES or job.state == JobState.STAGING:
            db.transition(
                job.spec.job_id, JobState.INTERRUPTED, "Agent restarted before completion"
            )
        elif job.state == JobState.CREATED:
            db.transition(job.spec.job_id, JobState.FAILED, "Agent restarted before staging")
