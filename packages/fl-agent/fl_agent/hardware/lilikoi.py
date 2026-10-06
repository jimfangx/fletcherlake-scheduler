"""Map trusted argv templates to firmware utilities without assuming their CLI syntax.

Templates support named fields: {bitstream}, {elf}, {rail}, {volts}, {channel}, {hz}.
Each replacement remains a single argv item. discover and wait_for_completion emit JSON;
read_voltage/read_current emit a number, and read_uart emits raw bytes. Scripts are supplied
by the hardware team. Missing operations fail explicitly instead of simulating success.
"""

import json
import re
from pathlib import Path

from fl_common.errors import PlatformError

from .base import RunResult
from .process import CommandResult, run_command

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)\}")


class LilikoiBoardBackend:
    def __init__(
        self,
        commands: dict[str, list[str]],
        timeout: float = 60,
        devices: dict[str, str] | None = None,
    ) -> None:
        self.commands = commands
        self.timeout = timeout
        self.devices = devices or {}

    async def _call(self, operation: str, **values: object) -> CommandResult:
        template = self.commands.get(operation)
        if not template:
            raise PlatformError("UNSUPPORTED_OPERATION", f"No firmware command for {operation}")
        try:
            substitutions = {**self.devices, **values}
            argv = [
                PLACEHOLDER.sub(lambda match: str(substitutions[match.group(1)]), part)
                for part in template
            ]
        except (KeyError, ValueError) as error:
            raise PlatformError(
                "INVALID_COMMAND_TEMPLATE", str(error), operation=operation
            ) from error
        return await run_command(argv, timeout=self.timeout)

    async def discover(self) -> dict[str, str]:
        data = json.loads((await self._call("discover")).stdout)
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
        return float((await self._call("read_voltage", channel=channel)).stdout)

    async def read_current(self, channel: int) -> float:
        return float((await self._call("read_current", channel=channel)).stdout)

    async def set_frequency(self, hz: float) -> None:
        await self._call("set_frequency", hz=hz)

    async def program_fpga(self, bitstream: Path) -> None:
        await self._call("program_fpga", bitstream=bitstream)

    async def program_soc(self, elf: Path) -> None:
        await self._call("program_soc", elf=elf)

    async def start(self) -> None:
        await self._call("start")

    async def stop(self) -> None:
        await self._call("stop")

    async def read_uart(self) -> bytes:
        return (await self._call("read_uart")).stdout

    async def wait_for_completion(self) -> RunResult:
        return RunResult.model_validate_json((await self._call("wait_for_completion")).stdout)
