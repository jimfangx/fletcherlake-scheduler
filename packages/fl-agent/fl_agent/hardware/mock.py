"""Deterministic hardware simulator with controllable failures and hangs."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from fl_common.errors import PlatformError

from .base import RunResult


@dataclass
class MockBehavior:
    programming_seconds: float = 0.01
    run_seconds: float = 0.02
    passed: bool = True
    hang: bool = False
    fail_operation: str | None = None
    minimum_voltage: float = 0.75
    maximum_frequency: float = 250_000_000
    uart: bytes = b"mock: hello from SoC\n"


class MockBoardBackend:
    def __init__(self, behavior: MockBehavior | None = None) -> None:
        self.behavior = behavior or MockBehavior()
        self.powered = False
        self.running = False
        self.voltages: dict[int, float] = {0: 0.9}
        self.frequency = 100_000_000.0
        self.calls: list[str] = []
        self.fpga_program_count = 0
        self.soc_program_count = 0
        self._uart_sent = False
        self._stopped = asyncio.Event()

    async def _operation(self, name: str, delay: float = 0) -> None:
        self.calls.append(name)
        if self.behavior.fail_operation == name:
            raise PlatformError("HARDWARE_ERROR", f"Simulated {name} failure", operation=name)
        await asyncio.sleep(delay)

    async def discover(self) -> dict[str, str]:
        await self._operation("discover")
        return {"backend": "mock"}

    async def power_on(self) -> None:
        await self._operation("power_on")
        if not self.powered:
            self.voltages = {0: 0.9}
            self.frequency = 100_000_000.0
        self.powered = True

    async def power_off(self) -> None:
        await self._operation("power_off")
        self.powered = False
        self.running = False
        self._stopped.set()

    async def set_voltage(self, rail: int, volts: float) -> None:
        await self._operation("set_voltage")
        self.voltages[rail] = volts

    async def read_voltage(self, channel: int) -> float:
        await self._operation("read_voltage")
        return self.voltages.get(channel, 0)

    async def read_current(self, channel: int) -> float:
        await self._operation("read_current")
        return 0.1 if self.powered else 0

    async def set_frequency(self, hz: float) -> None:
        await self._operation("set_frequency")
        self.frequency = hz

    async def program_fpga(self, bitstream: Path) -> None:
        await self._operation("program_fpga", self.behavior.programming_seconds)
        if not await asyncio.to_thread(bitstream.is_file):
            raise FileNotFoundError(bitstream)
        self.fpga_program_count += 1

    async def program_soc(self, elf: Path) -> None:
        await self._operation("program_soc", self.behavior.programming_seconds)
        if not await asyncio.to_thread(elf.is_file):
            raise FileNotFoundError(elf)
        self.soc_program_count += 1

    async def start(self) -> None:
        await self._operation("start")
        if not self.powered:
            raise PlatformError("BOARD_OFF", "Cannot start an unpowered board")
        self.running = True
        self._uart_sent = False
        self._stopped.clear()

    async def stop(self) -> None:
        await self._operation("stop")
        self.running = False
        self._stopped.set()

    async def read_uart(self) -> bytes:
        await self._operation("read_uart")
        if self.running and not self._uart_sent:
            self._uart_sent = True
            return self.behavior.uart
        return b""

    async def wait_for_completion(self) -> RunResult:
        await self._operation("wait_for_completion")
        if self.behavior.hang:
            await self._stopped.wait()
        else:
            await asyncio.sleep(self.behavior.run_seconds)
        passed = (
            self.behavior.passed
            and min(self.voltages.values()) >= self.behavior.minimum_voltage
            and self.frequency <= self.behavior.maximum_frequency
        )
        self.running = False
        return RunResult(passed=passed, exit_code=0 if passed else 1)
