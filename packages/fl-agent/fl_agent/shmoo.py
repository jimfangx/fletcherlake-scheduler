"""Independent discrete voltage/frequency searches with recorded sampled points."""

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Protocol

from fl_common.errors import PlatformError
from fl_common.models import ShmooConfig, SweepRange
from fl_common.models.base import Schema
from pydantic import Field

from .hardware.base import BoardBackend


class ShmooSample(Schema):
    axis: str
    value: float
    passed: bool


class ShmooResult(Schema):
    minimum_passing_voltage: float | None = None
    maximum_passing_frequency: float | None = None
    samples: list[ShmooSample] = Field(default_factory=list)


class ShmooStrategy(Protocol):
    async def execute(
        self,
        config: ShmooConfig,
        backend: BoardBackend,
        trial: Callable[[], Awaitable[bool]],
        record_sample: Callable[[ShmooSample], None] | None = None,
    ) -> ShmooResult: ...


def candidates(bounds: SweepRange) -> list[float]:
    low, high, step = (Decimal(str(value)) for value in (bounds.low, bounds.high, bounds.step))
    count = int((high - low) // step) + 1
    return [float(low + index * step) for index in range(count)]


class DiscreteBinarySearch:
    """Assume monotonic pass/fail behavior on each independent axis.

    Voltage trials hold frequency at its lowest requested value. Frequency trials use the
    highest requested voltage, rather than claiming a combined 2-D operating limit.
    """

    async def execute(
        self,
        config: ShmooConfig,
        backend: BoardBackend,
        trial: Callable[[], Awaitable[bool]],
        record_sample: Callable[[ShmooSample], None] | None = None,
    ) -> ShmooResult:
        result = ShmooResult()
        if config.frequency:
            await backend.set_frequency(config.frequency.low)
        if config.voltage:
            values = candidates(config.voltage)
            result.minimum_passing_voltage = await self._search(
                "voltage",
                values,
                True,
                backend,
                config.rail,
                trial,
                result,
                record_sample,
            )
            await backend.set_voltage(config.rail, values[-1])
        if config.frequency:
            values = candidates(config.frequency)
            result.maximum_passing_frequency = await self._search(
                "frequency",
                values,
                False,
                backend,
                config.rail,
                trial,
                result,
                record_sample,
            )
        return result

    async def _search(
        self,
        axis: str,
        values: list[float],
        minimum: bool,
        backend: BoardBackend,
        rail: int,
        trial: Callable[[], Awaitable[bool]],
        result: ShmooResult,
        record_sample: Callable[[ShmooSample], None] | None,
    ) -> float:
        async def sample(index: int) -> bool:
            if minimum:
                await backend.set_voltage(rail, values[index])
            else:
                await backend.set_frequency(values[index])
            passed = await trial()
            sampled = ShmooSample(axis=axis, value=values[index], passed=passed)
            result.samples.append(sampled)
            if record_sample:
                record_sample(sampled)
            return passed

        known = len(values) - 1 if minimum else 0
        if not await sample(known):
            raise PlatformError(
                "BINARY_FAILED_TO_RUN",
                f"Binary failed at baseline {axis}",
                samples=[sample.model_dump(mode="json") for sample in result.samples],
            )
        low, high = (0, known) if minimum else (known, len(values) - 1)
        while low < high:
            middle = (low + high) // 2 if minimum else (low + high + 1) // 2
            passed = await sample(middle)
            if minimum:
                if passed:
                    high = middle
                else:
                    low = middle + 1
            elif passed:
                low = middle
            else:
                high = middle - 1
        return values[low]
