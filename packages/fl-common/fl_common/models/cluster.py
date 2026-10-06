"""Mac inventory, detected tools, and optional one-way power-control scaffold."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .base import Schema
from .board import BoardConfig


class ToolInfo(Schema):
    path: str
    version: str | None = None
    edition: Literal["lab", "full"] | None = None


class EnvironmentConfig(Schema):
    vivado: ToolInfo | None = None
    openocd: ToolInfo | None = None
    openfpgaloader: ToolInfo | None = None
    riscv_toolchain: ToolInfo | None = None
    gcc: ToolInfo | None = None
    clang: ToolInfo | None = None
    bbcp: ToolInfo | None = None
    chipyard: str | None = None


class OSInfo(Schema):
    name: str
    release: str
    build: str | None = None


class PowerControlConfig(Schema):
    type: Literal["kasa"] = "kasa"
    device_id: str | None = None
    host: str | None = None
    alias: str | None = None


class ClusterState(StrEnum):
    CONFIGURATION_INCOMPLETE = "CONFIGURATION_INCOMPLETE"
    READY = "READY"
    RECONFIGURING = "RECONFIGURING"
    DRAINING = "DRAINING"
    RESTARTING = "RESTARTING"
    DESTROYED = "DESTROYED"
    FORCE_POWER_OFF_PENDING = "FORCE_POWER_OFF_PENDING"
    POWERED_OFF = "POWERED_OFF"


class ClusterConfig(Schema):
    cluster_id: UUID | None = None
    boards: list[BoardConfig | None] = Field(min_length=1, max_length=3)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    os: OSInfo
    apple_model: str
    mac_address: str
    power_control: PowerControlConfig | None = None

    @model_validator(mode="after")
    def unique_boards(self) -> "ClusterConfig":
        ids = [board.board_id for board in self.boards if board is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("board IDs must be unique within a cluster")
        return self
