import asyncio
import os
import sys
from pathlib import Path

import pytest
from fl_agent.hardware.lilikoi import LilikoiBoardBackend
from fl_agent.hardware.process import run_command
from fl_common.errors import PlatformError


async def test_command_captures_structured_failure_and_literal_arguments() -> None:
    literal = "$(touch /tmp/should-never-exist); 'quoted'"
    result = await run_command([sys.executable, "-c", "import sys; print(sys.argv[1])", literal])
    assert result.stdout.decode().strip() == literal
    with pytest.raises(PlatformError) as error:
        await run_command(
            [sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(3)"]
        )
    assert error.value.details["returncode"] == 3
    assert "bad" in error.value.details["stderr"]


async def test_timeout_reaps_firmware() -> None:
    with pytest.raises(PlatformError) as error:
        await run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05)
    assert error.value.code == "COMMAND_TIMED_OUT"


async def test_cancellation_kills_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    )
    task = asyncio.create_task(run_command([sys.executable, "-c", script, str(pid_file)]))
    async with asyncio.timeout(5):
        # Poll external process state; an in-process Event cannot signal its file write.
        while not await asyncio.to_thread(pid_file.exists):  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_firmware_mapping_and_missing_operation() -> None:
    backend = LilikoiBoardBackend(
        {
            "set_frequency": [
                sys.executable,
                "-c",
                "import sys; assert sys.argv[1] == '250000000'",
                "{hz}",
            ],
            "read_voltage": [sys.executable, "-c", "print(0.85)"],
            "wait_for_completion": [
                sys.executable,
                "-c",
                'print(\'{"passed":true,"exit_code":0}\')',
            ],
        }
    )
    await backend.set_frequency(250000000)
    assert await backend.read_voltage(0) == 0.85
    assert (await backend.wait_for_completion()).passed
    with pytest.raises(PlatformError) as error:
        await backend.power_on()
    assert error.value.code == "UNSUPPORTED_OPERATION"


async def test_firmware_templates_use_named_device_mapping() -> None:
    backend = LilikoiBoardBackend(
        {
            "read_current": [
                sys.executable,
                "-c",
                "import sys; assert sys.argv[1] == '/dev/device with spaces'; print(0.25)",
                "{uart}",
            ]
        },
        devices={"uart": "/dev/device with spaces"},
    )
    assert await backend.read_current(0) == 0.25
