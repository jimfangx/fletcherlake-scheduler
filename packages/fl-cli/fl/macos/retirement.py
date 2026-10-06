"""Durable destroy intent outside the active directory, surviving its atomic archival."""

import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from fl_agent.retired_state import prepare_root
from fl_common.errors import PlatformError
from fl_common.locks import ExclusiveLock


def management_lock(state: Path) -> ExclusiveLock:
    return ExclusiveLock(prepare_root(state) / "management.lock")


def require_no_retirement(state: Path) -> None:
    if (prepare_root(state) / "pending-destroy.json").exists():
        raise PlatformError(
            "DESTROY_PENDING", "Retry cluster destroy before another lifecycle operation"
        )


@contextmanager
def stopped_agent(state: Path, runtime: Path, *, timeout: float = 30) -> Iterator[None]:
    deadline = time.monotonic() + timeout
    while True:
        stack = ExitStack()
        try:
            stack.enter_context(ExclusiveLock(state / "agent.lock"))
            stack.enter_context(ExclusiveLock(runtime / "agent.lock"))
            break
        except PlatformError as error:
            stack.close()
            if error.code != "ALREADY_OWNED":
                raise
            if time.monotonic() >= deadline:
                raise PlatformError(
                    "AGENT_STILL_RUNNING", "Wait for the daemon to stop, then retry destroy"
                ) from error
            time.sleep(0.1)
    try:
        yield
    finally:
        stack.close()
