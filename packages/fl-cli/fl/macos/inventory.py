"""One inventory parser with explicit > supplied interactive values > detection precedence."""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from fl_agent.detection import detect_host, merge_overrides
from fl_common.errors import PlatformError
from fl_common.models import ClusterConfig

InventoryInput = Path | Mapping[str, Any] | ClusterConfig


def read_overrides(source: InventoryInput) -> dict[str, Any]:
    if isinstance(source, ClusterConfig):
        return source.model_dump(mode="json")
    data = yaml.safe_load(source.read_text()) if isinstance(source, Path) else dict(source)
    if not isinstance(data, dict) or "boards" not in data:
        raise PlatformError("INVALID_INVENTORY", "Configuration must explicitly provide boards")
    return data


def inventory(
    source: InventoryInput,
    *,
    interactive: Mapping[str, Any] | None = None,
    detector: Callable[[], dict[str, Any]] = detect_host,
) -> ClusterConfig:
    if isinstance(source, ClusterConfig):
        # An already resolved model preserves explicit nulls and does not rerun host detection.
        return source.model_copy(deep=True)
    detected = merge_overrides(detector(), dict(interactive or {}))
    return ClusterConfig.model_validate(merge_overrides(detected, read_overrides(source)))
