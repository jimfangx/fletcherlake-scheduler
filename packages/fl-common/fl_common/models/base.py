"""Strict, versioned wire schemas and UTC timestamp helpers."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


def utcnow() -> datetime:
    return datetime.now(UTC)


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1
