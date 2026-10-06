import pytest
from fl_agent.hardware.mock import MockBoardBackend
from fl_agent.shmoo import DiscreteBinarySearch, candidates
from fl_common.errors import PlatformError
from fl_common.models import ShmooConfig, SweepRange


async def test_shmoo_discrete_limits_and_every_sample_recorded() -> None:
    board = MockBoardBackend()
    await board.power_on()

    async def trial() -> bool:
        await board.start()
        result = await board.wait_for_completion()
        await board.stop()
        return result.passed

    result = await DiscreteBinarySearch().execute(
        ShmooConfig(
            voltage=SweepRange(low=0.7, high=0.9, step=0.05),
            frequency=SweepRange(low=100_000_000, high=400_000_000, step=50_000_000),
        ),
        board,
        trial,
    )
    assert result.minimum_passing_voltage == 0.75
    assert result.maximum_passing_frequency == 250_000_000
    assert result.samples[0].axis == "voltage" and result.samples[0].value == 0.9
    assert next(s.value for s in result.samples if s.axis == "frequency") == 100_000_000
    assert any(not sample.passed for sample in result.samples)


async def test_baseline_failure_aborts_and_preserves_sample() -> None:
    board = MockBoardBackend()
    calls = 0

    async def fail() -> bool:
        nonlocal calls
        calls += 1
        return False

    with pytest.raises(PlatformError) as error:
        await DiscreteBinarySearch().execute(
            ShmooConfig(voltage=SweepRange(low=0.5, high=0.6, step=0.1)),
            board,
            fail,
        )
    assert error.value.code == "BINARY_FAILED_TO_RUN"
    assert calls == 1
    assert error.value.details["samples"][0]["value"] == 0.6


def test_candidate_grid_has_no_float_accumulation() -> None:
    assert candidates(SweepRange(low=0.7, high=0.9, step=0.05)) == [0.7, 0.75, 0.8, 0.85, 0.9]
    assert candidates(SweepRange(low=1, high=2, step=0.6)) == [1, 1.6]
