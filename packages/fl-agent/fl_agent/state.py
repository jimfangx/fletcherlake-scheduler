"""Explicit legal transitions shared by transactional state updates."""

from fl_common.errors import PlatformError
from fl_common.models.events import ACTIVE_STATES, JobState

_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.CREATED: frozenset({JobState.STAGING, JobState.FAILED, JobState.CANCELING}),
    JobState.STAGING: frozenset(
        {
            JobState.QUEUED,
            JobState.FAILED,
            JobState.CANCELING,
            JobState.INTERRUPTED,
        }
    ),
    JobState.QUEUED: frozenset({JobState.PREPARING, JobState.CANCELING}),
    JobState.PREPARING: frozenset(
        {
            JobState.PROGRAMMING_FPGA,
            JobState.PROGRAMMING_SOC,
            JobState.RUNNING,
        }
    ),
    JobState.PROGRAMMING_FPGA: frozenset({JobState.PROGRAMMING_SOC, JobState.RUNNING}),
    JobState.PROGRAMMING_SOC: frozenset({JobState.RUNNING}),
    JobState.RUNNING: frozenset({JobState.SUCCEEDED}),
    JobState.CANCELING: frozenset({JobState.CANCELED, JobState.INTERRUPTED, JobState.FAILED}),
}
_FAILURES = frozenset(
    {
        JobState.FAILED,
        JobState.TIMED_OUT,
        JobState.CANCELING,
        JobState.INTERRUPTED,
        JobState.LOST,
        JobState.INTERRUPTED_BY_FORCE_POWEROFF,
    }
)


def validate_transition(old: JobState, new: JobState) -> None:
    allowed = _TRANSITIONS.get(old, frozenset())
    if old in ACTIVE_STATES and old != JobState.CANCELING:
        allowed = allowed | _FAILURES
    if new not in allowed:
        raise PlatformError("INVALID_TRANSITION", f"Cannot transition {old} to {new}")
