import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fl_agent.hardware.lilikoi import LilikoiBoardBackend
from fl_agent.hardware.process import run_command
from fl_common.errors import PlatformError
from lilikoi import fakes


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


FAKES = Path(fakes.__file__).parent


@pytest.fixture
def fake_mcu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """lilikoi's fake RP2350 on a pty; yields the device path the daemon opens."""
    # The fake MCU reports FPGA DONE while this file exists; the fake openocd (spawned by the
    # daemon, which inherits this process's environment) creates it.
    monkeypatch.setenv("FAKE_FPGA_DONE_FILE", str(tmp_path / "fpga.done"))
    # The fake must import the same lilikoi the backend uses, whatever the site layout.
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([str(FAKES.parents[1]), os.environ.get("PYTHONPATH", "")]),
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "lilikoi.fakes.fake_mcu"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
    )
    assert process.stdout is not None
    port = process.stdout.readline().strip()
    assert port.startswith("/dev/"), "fake MCU did not start"
    yield port
    process.terminate()
    process.wait(timeout=5)


@pytest.fixture
async def lilikoi_board(fake_mcu: str, tmp_path: Path) -> AsyncIterator[LilikoiBoardBackend]:
    """A LilikoiBoardBackend driving the fake RP2350, fake openocd and fake uart_tsi."""
    cfg = tmp_path / "fletcherlake.cfg"
    cfg.write_text("# fake adapter cfg\n")
    backend = LilikoiBoardBackend(
        {
            "mcu": fake_mcu,
            "run_dir": str(tmp_path / "run"),
            "tsi_tty": "/dev/null",
            "uart_tsi": str(FAKES / "fake_uart_tsi.py"),
            "openocd": str(FAKES / "fake_openocd.py"),
            "openocd_cfg": str(cfg),
            "rail_limits": "650-900,650-900,650-900",
            "core_sel": "own,own",
        },
        timeout=30,
    )
    try:
        yield backend
    finally:
        await backend.shutdown()


async def test_lilikoi_job_flow_against_fake_board(
    lilikoi_board: LilikoiBoardBackend, tmp_path: Path
) -> None:
    backend = lilikoi_board
    discovered = await backend.discover()
    assert discovered["backend"] == "lilikoi" and discovered["fpga_done"] == "0"
    await backend.power_on()
    bitstream = tmp_path / "top.bit"
    bitstream.write_bytes(b"\xff" * 32)
    await backend.program_fpga(bitstream)
    assert (await backend.discover())["fpga_done"] == "1"
    await backend.set_voltage(1, 0.75)
    assert await backend.read_voltage(1) == 0.75
    assert await backend.read_current(1) > 0
    elf = tmp_path / "hello.elf"
    elf.write_bytes(b"\x7fELF" + b"\0" * 60)
    await backend.program_soc(elf)
    await backend.start()
    output = b""
    async with asyncio.timeout(5):
        while b"hello from fake soc" not in output:
            output += await backend.read_uart()
            await asyncio.sleep(0.02)
    result = await backend.wait_for_completion()
    assert result.passed and result.exit_code == 0 and "uart_tsi exited 0" in (result.detail or "")
    await backend.stop()
    await backend.stop()  # idempotent
    await backend.power_off()
    assert await backend.read_voltage(1) == 0.0


async def test_lilikoi_errors_become_platform_errors(
    lilikoi_board: LilikoiBoardBackend, tmp_path: Path
) -> None:
    backend = lilikoi_board
    with pytest.raises(PlatformError) as error:
        await backend.start()
    assert error.value.code == "NO_PROGRAM" and error.value.details["operation"] == "start"
    with pytest.raises(PlatformError) as error:
        await backend.set_frequency(1e6)
    assert error.value.code == "UNSUPPORTED"
    # The deadline is enforced in the agent, not by the daemon: a hanging run times out and
    # stop still cleans it up through the same daemon.
    hanging = tmp_path / "hang.elf"
    hanging.write_bytes(b"\x7fELF" + b"\0" * 60)
    await backend.program_soc(hanging)
    await backend.start()
    impatient = LilikoiBoardBackend({"mcu": backend.mcu, "run_dir": str(backend.run_dir)}, 0.2)
    with pytest.raises(PlatformError) as error:
        await impatient.wait_for_completion()
    assert error.value.code == "COMMAND_TIMED_OUT"
    await backend.stop()


def test_lilikoi_device_mapping_is_validated() -> None:
    with pytest.raises(PlatformError) as error:
        LilikoiBoardBackend({"tsi_tty": "/dev/cu.board"})
    assert error.value.code == "INVALID_DEVICE_MAPPING"
    with pytest.raises(PlatformError) as error:
        LilikoiBoardBackend({"mcu": "ABC", "uart": "/dev/cu.board"})
    assert "uart" in error.value.message and "tsi_tty" in error.value.details["allowed"]
    backend = LilikoiBoardBackend({"mcu": "ABC", "run_dir": "/tmp/x", "core_sel": "own,own"})
    assert backend.settings == {"core_sel": "own,own"} and backend.run_dir == Path("/tmp/x")
