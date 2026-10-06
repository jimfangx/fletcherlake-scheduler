"""Cancellable argv-only subprocesses on macOS and Linux."""

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from fl_common.errors import PlatformError


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill the whole process group, including helpers holding output pipes open."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        pass
    # A parent may exit while a child ignores TERM. Always reap the remaining group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def run_command(
    argv: list[str],
    *,
    timeout: float = 60,  # noqa: ASYNC109 -- caller requires a per-command deadline
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    if not argv or timeout <= 0:
        raise ValueError("Command requires nonempty argv and positive timeout")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except OSError as error:
        raise PlatformError("COMMAND_START_FAILED", str(error), argv=argv) from error
    communication = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout)
    except (TimeoutError, asyncio.CancelledError) as error:
        await _terminate(process)
        stdout, stderr = await communication
        if isinstance(error, asyncio.CancelledError):
            raise
        raise PlatformError(
            "COMMAND_TIMED_OUT",
            "Command timed out",
            argv=argv,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        ) from error
    result = CommandResult(tuple(argv), process.returncode or 0, stdout, stderr)
    if result.returncode:
        raise PlatformError(
            "COMMAND_FAILED",
            "Command failed",
            argv=argv,
            returncode=result.returncode,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )
    return result
