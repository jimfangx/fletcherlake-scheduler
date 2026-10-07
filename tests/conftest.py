"""Shared explicit mock inventory and daemon lifecycle fixtures."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from fl_agent.configuration import confirm_config, write_config
from fl_agent.hardware.base import BoardBackend
from fl_agent.service import AgentService
from fl_common.models import BoardConfig, ClusterConfig, FPGAConfig, OSInfo, SoCConfig


@pytest.fixture
def config() -> ClusterConfig:
    return ClusterConfig(
        boards=[
            BoardConfig(
                board_id=f"board-{index}",
                backend="mock",
                num_vrails=1,
                num_vsense=1,
                fpgas=[FPGAConfig(model="xcvu9p")],
                socs=[SoCConfig(name="fletcherlake")],
            )
            for index in range(3)
        ],
        os=OSInfo(name="Darwin", release="24.0"),
        apple_model="Macmini9,1",
        mac_address="00:11:22:33:44:55",
    )


@pytest.fixture
def input_files(tmp_path: Path) -> dict[str, str]:
    binary, bitstream = tmp_path / "hello.elf", tmp_path / "fpga.bit"
    binary.write_bytes(b"fake ELF for simulation")
    bitstream.write_bytes(b"fake bitstream for simulation")
    return {"binary": str(binary), "bitstream": str(bitstream)}


@pytest.fixture
async def service_factory(
    tmp_path: Path,
    config: ClusterConfig,
) -> AsyncIterator[Callable[..., AgentService]]:
    services: list[AgentService] = []

    def make(
        backends: dict[str, BoardBackend] | None = None, confirmed: bool = True
    ) -> AgentService:
        root = tmp_path / f"agent-{len(services)}"
        write_config(root, config)
        if confirmed:
            confirm_config(root)
        service = AgentService(config, root, root / "run", backends)
        services.append(service)
        return service

    yield make
    for service in services:
        if hasattr(service, "db"):
            await service.stop()


# Fixtures remain split by responsibility; importing as a plugin registers PostgreSQL helpers.
pytest_plugins = [
    "tests.postgres",
    "tests.auth",
    "tests.headscale",
    "tests.tailscale",
    "tests.rclone",
    "tests.connected",
    "tests.artifact_exports",
    "tests.retirement_stack",
    "tests.private_stack",
]
