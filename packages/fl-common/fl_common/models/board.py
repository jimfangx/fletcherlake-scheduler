"""Explicit physical inventory; detection never invents board definitions."""

from typing import Annotated, Literal

from pydantic import Field

from .base import Schema

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]


class FPGAConfig(Schema):
    model: str = Field(min_length=1)
    device: str | None = None


class SoCConfig(Schema):
    name: str = Field(min_length=1)
    device: str | None = None


class BoardConfig(Schema):
    board_id: Identifier
    device_mapping: dict[str, str] = Field(default_factory=dict)
    num_vrails: int = Field(default=0, ge=0)
    num_vsense: int = Field(default=0, ge=0)
    num_isense: int = Field(default=0, ge=0)
    clock_source: Literal["on_chip", "fpga", "external_clock_gen"] = "on_chip"
    fpgas: list[FPGAConfig] = Field(default_factory=list)
    socs: list[SoCConfig] = Field(default_factory=list)
    backend: Literal["mock", "lilikoi"]
    # Firmware argv templates belong to trusted cluster configuration, never to JobConfig.
    firmware_commands: dict[str, list[str]] = Field(default_factory=dict)
    firmware_timeout_seconds: int = Field(default=60, gt=0, le=35 * 86400)
