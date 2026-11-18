from pathlib import Path

import pytest
import yaml
from fl_agent.hardware.base import BoardBackend
from fl_agent.hardware.lilikoi import LilikoiBoardBackend
from fl_agent.hardware.mock import MockBoardBackend
from fl_agent.hardware.sophialake import SophiaLakeBoardBackend
from fl_agent.service import AgentService
from fl_common.errors import PlatformError
from fl_common.models import BoardConfig, JobConfig, ResourceConstraints
from fl_common.scheduling import matches

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.parametrize(
    "name,devices,expected",
    [
        ("sophialake", {"chip_cfg": "/opt/boards/chip.cfg"}, SophiaLakeBoardBackend),
        ("lilikoi", {"mcu": "test-serial"}, LilikoiBoardBackend),
        ("mock", {}, MockBoardBackend),
    ],
)
def test_factory_selects_requested_backend(name, devices, expected):
    board = BoardConfig.model_validate(
        {"board_id": "test-board", "backend": name, "device_mapping": devices}
    )
    backend: BoardBackend = AgentService._make_backend(board)
    assert isinstance(backend, expected)
    if isinstance(backend, SophiaLakeBoardBackend):
        assert backend.devices == devices and backend.devices is not devices
        assert backend.timeout == board.firmware_timeout_seconds


def test_factory_never_falls_back_to_mock_for_unknown_backend():
    board = BoardConfig.model_construct(board_id="test-board", backend="unknown")
    with pytest.raises(PlatformError) as error:
        AgentService._make_backend(board)
    assert error.value.code == "INVALID_BACKEND"


@pytest.mark.parametrize(
    "operation,args",
    [
        ("discover", ()),
        ("power_on", ()),
        ("power_off", ()),
        ("set_voltage", (0, 0.8)),
        ("read_voltage", (0,)),
        ("read_current", (0,)),
        ("set_frequency", (100_000_000,)),
        ("program_fpga", (Path("top.bit"),)),
        ("program_soc", (Path("app.elf"),)),
        ("start", ()),
        ("stop", ()),
        ("read_uart", ()),
        ("wait_for_completion", ()),
    ],
)
async def test_template_never_reports_success_without_implementation(operation, args):
    backend = SophiaLakeBoardBackend({})
    with pytest.raises(PlatformError) as error:
        await getattr(backend, operation)(*args)
    assert error.value.code == "BACKEND_NOT_IMPLEMENTED"
    assert error.value.details == {"backend": "sophialake", "operation": operation}


def test_example_job_selects_only_the_named_board():
    inventory = yaml.safe_load((EXAMPLES / "cluster-sophialake.yaml").read_text())
    board = BoardConfig.model_validate(inventory["boards"][0])
    job = JobConfig.from_yaml(EXAMPLES / "job-sophialake.yaml")
    assert matches(board, job.resource_constraints)
    other = board.model_copy(update={"board_id": "sophialake-1"})
    assert not matches(other, job.resource_constraints)
    # A backend name is not a SoC label and cannot implicitly match one.
    assert not matches(board, ResourceConstraints(soc="sophialake"))


async def test_unimplemented_board_is_error_while_other_board_is_ready(config, service_factory):
    config.boards = [BoardConfig(board_id="sophialake-0", backend="sophialake"), config.boards[0]]
    service = service_factory()
    await service.start()
    boards = {board["board_id"]: board for board in service.db.boards()}
    assert boards["sophialake-0"]["state"] == "ERROR"
    assert "not implemented" in boards["sophialake-0"]["error"]
    assert boards["board-0"]["state"] == "IDLE"
    assert service.workers["sophialake-0"]._task is None
