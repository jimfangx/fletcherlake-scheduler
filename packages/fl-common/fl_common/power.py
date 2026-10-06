"""Integration point for future emergency force-off. No power-on operation exists."""

from typing import Protocol

from fl_common.models import ClusterConfig


class ClusterPowerController(Protocol):
    async def force_power_off(self, cluster: ClusterConfig) -> None: ...


class UnsupportedPowerController:
    async def force_power_off(self, cluster: ClusterConfig) -> None:
        raise NotImplementedError("Smart-plug control is intentionally scaffold-only")
