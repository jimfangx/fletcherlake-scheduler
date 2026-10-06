"""User configuration is separate from trusted identity and placement metadata."""

from pathlib import Path
from uuid import UUID, uuid4

import yaml
from pydantic import AwareDatetime, Field, model_validator

from .artifact import ArtifactRef
from .base import Schema, utcnow

MAX_TIMEOUT = 35 * 24 * 60 * 60


class ResourceConstraints(Schema):
    board: str | None = None
    soc: str | None = None
    fpga: str | None = None


class SweepRange(Schema):
    low: float = Field(gt=0)
    high: float = Field(gt=0)
    step: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> "SweepRange":
        if self.low > self.high:
            raise ValueError("shmoo low must not exceed high")
        if (self.high - self.low) / self.step > 1_000_000:
            raise ValueError("shmoo exceeds one million candidate points")
        return self


class ShmooConfig(Schema):
    voltage: SweepRange | None = None
    frequency: SweepRange | None = None
    rail: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def has_axis(self) -> "ShmooConfig":
        if self.voltage is None and self.frequency is None:
            raise ValueError("shmoo requires at least one axis")
        return self


class JobConfig(Schema):
    priority: int = Field(default=0, ge=-20, le=19)
    resource_constraints: ResourceConstraints = Field(default_factory=ResourceConstraints)
    run_timeout: int = Field(default=3600, gt=0, le=MAX_TIMEOUT)
    run_collateral_ttl: int = Field(default=30, ge=0, le=365)
    binary: str | None = Field(default=None, min_length=1)
    bitstream: str | None = Field(default=None, min_length=1)
    force_reflash: bool = False
    shmoo: ShmooConfig | None = None

    @classmethod
    def from_yaml(cls, path: Path, overrides: dict[str, object] | None = None) -> "JobConfig":
        """Resolve input paths relative to the YAML file, not the caller's directory."""
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("job config must be a YAML mapping")
        data.update(overrides or {})
        config = cls.model_validate(data)
        for kind in ("binary", "bitstream"):
            value = getattr(config, kind)
            if value is not None:
                setattr(config, kind, str((path.parent / value).resolve()))
        return config


class JobSpec(Schema):
    job_id: UUID
    owner: str = Field(min_length=1)
    submitted_at: AwareDatetime
    priority: int = Field(ge=-20, le=19)
    resource_constraints: ResourceConstraints
    run_timeout_seconds: int = Field(gt=0, le=MAX_TIMEOUT)
    collateral_ttl_days: int = Field(ge=0, le=365)
    binary: ArtifactRef | None = None
    bitstream: ArtifactRef | None = None
    force_reflash: bool = False
    shmoo: ShmooConfig | None = None

    @model_validator(mode="after")
    def artifact_kinds(self) -> "JobSpec":
        if self.binary is not None and self.binary.kind != "binary":
            raise ValueError("binary reference must have kind=binary")
        if self.bitstream is not None and self.bitstream.kind != "bitstream":
            raise ValueError("bitstream reference must have kind=bitstream")
        return self

    @classmethod
    def from_config(
        cls,
        config: JobConfig,
        owner: str,
        *,
        job_id: UUID | None = None,
        binary: ArtifactRef | None = None,
        bitstream: ArtifactRef | None = None,
    ) -> "JobSpec":
        return cls(
            job_id=job_id or uuid4(),
            owner=owner,
            submitted_at=utcnow(),
            priority=config.priority,
            resource_constraints=config.resource_constraints,
            run_timeout_seconds=config.run_timeout,
            collateral_ttl_days=config.run_collateral_ttl,
            binary=binary,
            bitstream=bitstream,
            force_reflash=config.force_reflash,
            shmoo=config.shmoo,
        )
