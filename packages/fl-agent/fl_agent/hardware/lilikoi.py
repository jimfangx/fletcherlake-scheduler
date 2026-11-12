"""Drive a Fletcher Lake board through lilikoi, the hardware team's host tooling.

lilikoi is vendored at packages/lilikoi and owns everything board-specific: the RP2350 power
and sensing firmware protocol, OpenOCD bitstream programming and the UART-TSI program runner.
It runs one daemon per board; this backend speaks to that daemon over its unix socket from
inside the agent process, so no per-operation subprocess and no argv templates are needed.

`device_mapping` identifies the board and its tools. `mcu` (the RP2350 USB serial number or
device path) is required; `run_dir` places the daemon's socket, lock and logs; every other key
is a lilikoi setting such as `tsi_tty`, `uart_tsi`, `openocd_cfg`, `jtag_serial`,
`rail_limits` or `core_sel` (see `lilikoi.cli.SETTING_FLAGS`). Unknown keys are rejected so a
typo cannot silently fall back to a default.
"""

import asyncio
import base64
import os
from pathlib import Path
from typing import Any

from fl_common.errors import PlatformError
from lilikoi import client as lilikoi_client
from lilikoi.cli import DEFAULT_RUN_DIR, SETTING_FLAGS
from lilikoi.errors import LilikoiError

from .base import RunResult

MCU_KEY = "mcu"
RUN_DIR_KEY = "run_dir"
SETTING_KEYS = frozenset(SETTING_FLAGS) | {"selfcheck"}


def _absolute(path: Path) -> str:
    """The daemon's working directory is not the agent's; absolutize without touching disk."""
    return str(path if path.is_absolute() else Path(os.getcwd(), path))


class LilikoiBoardBackend:
    def __init__(self, devices: dict[str, str], timeout: float = 60) -> None:
        unknown = sorted(set(devices) - SETTING_KEYS - {MCU_KEY, RUN_DIR_KEY})
        if unknown:
            raise PlatformError(
                "INVALID_DEVICE_MAPPING",
                f"Unknown lilikoi device_mapping keys: {', '.join(unknown)}",
                allowed=sorted(SETTING_KEYS | {MCU_KEY, RUN_DIR_KEY}),
            )
        if not devices.get(MCU_KEY):
            raise PlatformError(
                "INVALID_DEVICE_MAPPING", "device_mapping.mcu (RP2350 serial number) is required"
            )
        self.mcu = devices[MCU_KEY]
        self.run_dir = Path(devices.get(RUN_DIR_KEY) or DEFAULT_RUN_DIR)
        self.settings: dict[str, Any] = {k: v for k, v in devices.items() if k in SETTING_KEYS}
        self.timeout = timeout

    async def _call(self, operation: str, **args: object) -> Any:
        try:
            return await asyncio.wait_for(
                lilikoi_client.request_async(
                    self.mcu, self.run_dir, operation, dict(args), self.settings
                ),
                self.timeout,
            )
        except LilikoiError as error:
            details = {"operation": operation, **error.details}
            raise PlatformError(error.code, error.message, **details) from error
        except TimeoutError as error:
            raise PlatformError(
                "COMMAND_TIMED_OUT",
                f"lilikoi {operation} exceeded {self.timeout} seconds",
                operation=operation,
            ) from error
        except OSError as error:
            raise PlatformError("DAEMON_UNAVAILABLE", str(error), operation=operation) from error

    async def discover(self) -> dict[str, str]:
        data = await self._call("discover")
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in data.items()
        ):
            raise PlatformError("INVALID_FIRMWARE_OUTPUT", "discover must return a string mapping")
        return data

    async def power_on(self) -> None:
        await self._call("power_on")

    async def power_off(self) -> None:
        await self._call("power_off")

    async def set_voltage(self, rail: int, volts: float) -> None:
        await self._call("set_voltage", rail=rail, volts=volts)

    async def read_voltage(self, channel: int) -> float:
        return float(await self._call("read_voltage", channel=channel))

    async def read_current(self, channel: int) -> float:
        return float(await self._call("read_current", channel=channel))

    async def set_frequency(self, hz: float) -> None:
        await self._call("set_frequency", hz=hz)

    async def program_fpga(self, bitstream: Path) -> None:
        await self._call("program_fpga", bitstream=_absolute(bitstream))

    async def program_soc(self, elf: Path) -> None:
        await self._call("program_soc", elf=_absolute(elf))

    async def start(self) -> None:
        await self._call("start")

    async def stop(self) -> None:
        await self._call("stop")

    async def read_uart(self) -> bytes:
        return base64.b64decode(await self._call("read_uart") or "")

    async def wait_for_completion(self) -> RunResult:
        return RunResult.model_validate(await self._call("wait_for_completion"))

    async def shutdown(self) -> None:
        """Stop the board's daemon (tests and bench use; the agent leaves it running)."""
        await self._call("shutdown")
