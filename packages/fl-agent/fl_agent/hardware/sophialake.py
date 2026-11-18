"""SophiaLake hardware implementation template.

Replace each stub with board-specific I/O while preserving the BoardBackend signatures.
See docs/board-backends.md for lifecycle, cancellation and completion requirements.
No hardware connections are opened by the constructor; every stub fails explicitly.
"""

from pathlib import Path
from typing import NoReturn

from fl_common.errors import PlatformError

from .base import RunResult


class SophiaLakeBoardBackend:
    def __init__(self, devices: dict[str, str], timeout: float = 60) -> None:
        # Define and validate SophiaLake device_mapping keys here as hardware is implemented.
        # Use stable device serials and absolute paths to tools/configs on the agent host.
        self.devices = dict(devices)
        self.timeout = timeout

    def _unimplemented(self, operation: str) -> NoReturn:
        raise PlatformError(
            "BACKEND_NOT_IMPLEMENTED",
            f"SophiaLake {operation} is not implemented; complete the hardware backend first",
            backend="sophialake",
            operation=operation,
        )

    async def discover(self) -> dict[str, str]:
        """Probe identity/health and return string-valued facts; do not invent inventory."""
        self._unimplemented("discover")

    async def power_on(self) -> None:
        """Enable power with the board's required sequencing and electrical limits."""
        self._unimplemented("power_on")

    async def power_off(self) -> None:
        """Idempotently leave hardware powered off; also called at agent startup."""
        self._unimplemented("power_off")

    async def set_voltage(self, rail: int, volts: float) -> None:
        """Set a zero-based rail in volts, enforcing validated board/chip limits."""
        self._unimplemented("set_voltage")

    async def read_voltage(self, channel: int) -> float:
        """Return a zero-based sensor channel's voltage in volts."""
        self._unimplemented("read_voltage")

    async def read_current(self, channel: int) -> float:
        """Return a zero-based sensor channel's current in amperes."""
        self._unimplemented("read_current")

    async def set_frequency(self, hz: float) -> None:
        """Set the execution clock in hertz, enforcing validated operating limits."""
        self._unimplemented("set_frequency")

    async def program_fpga(self, bitstream: Path) -> None:
        """Program the supplied local bitstream and wait for programming confirmation."""
        self._unimplemented("program_fpga")

    async def program_soc(self, elf: Path) -> None:
        """Load or stage the supplied local ELF; defer execution until start()."""
        self._unimplemented("program_soc")

    async def start(self) -> None:
        """Start the loaded/staged ELF and initialize console/completion tracking."""
        self._unimplemented("start")

    async def stop(self) -> None:
        """Idempotently stop execution and owned processes; also called at startup."""
        self._unimplemented("stop")

    async def read_uart(self) -> bytes:
        """Return newly available console bytes, or b'' when none are available."""
        self._unimplemented("read_uart")

    async def wait_for_completion(self) -> RunResult:
        """Wait for actual program completion and report its pass/fail outcome."""
        self._unimplemented("wait_for_completion")
