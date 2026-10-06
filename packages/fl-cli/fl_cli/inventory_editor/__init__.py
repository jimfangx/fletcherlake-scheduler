"""CLI-only editing; lifecycle and hardware operations remain in their shared services."""

from typing import Any

import typer
from fl_common.models import ClusterConfig

from .app import InventoryEditor


def edit_inventory(initial: dict[str, Any]) -> ClusterConfig:
    result = InventoryEditor(initial).run()
    if result is None:
        raise typer.Abort()
    return result


__all__ = ["InventoryEditor", "edit_inventory"]
