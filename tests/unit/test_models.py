from pathlib import Path
from uuid import uuid4

import pytest
from fl_common.models import (
    ArtifactRef,
    ClusterConfig,
    JobConfig,
    JobSpec,
    PowerControlConfig,
    ShmooConfig,
    SweepRange,
)
from fl_common.protocol import Message, MessageType
from pydantic import ValidationError


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_timeout", 0),
        ("run_timeout", 35 * 86400 + 1),
        ("run_collateral_ttl", -1),
        ("run_collateral_ttl", 366),
        ("priority", -21),
        ("owner", "spoofed"),
        ("job_id", str(uuid4())),
        ("assigned_board", "board-0"),
        ("date_submitted", "2020-01-01"),
        ("schema_version", 2),
    ],
)
def test_user_config_rejects_invalid_or_trusted_metadata(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        JobConfig.model_validate({field: value})


@pytest.mark.parametrize(
    "data",
    [
        {"low": 1, "high": 0.5, "step": 0.1},
        {"low": 0.5, "high": 1, "step": 0},
        {"low": 0.5, "high": float("inf"), "step": 0.1},
    ],
)
def test_shmoo_invalid_ranges(data: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        SweepRange.model_validate(data)


def test_empty_shmoo_rejected() -> None:
    with pytest.raises(ValidationError):
        ShmooConfig()


def test_roundtrips_and_trusted_spec(config: ClusterConfig) -> None:
    config.power_control = PowerControlConfig(device_id="lab-macmini-01")
    assert ClusterConfig.model_validate_json(config.model_dump_json()) == config
    job = JobSpec.from_config(JobConfig(run_timeout=35 * 86400, run_collateral_ttl=365), "jim")
    assert job.owner == "jim"
    assert job.submitted_at.tzinfo is not None
    assert JobSpec.model_validate_json(job.model_dump_json()) == job
    message = Message(type=MessageType.JOB_ENQUEUE, payload={"spec": job.model_dump(mode="json")})
    assert Message.model_validate_json(message.model_dump_json()) == message


def test_cluster_limits_and_duplicate_boards(config: ClusterConfig) -> None:
    with pytest.raises(ValidationError):
        config.boards = [config.boards[0]] * 4
    with pytest.raises(ValidationError):
        config.boards = [config.boards[0]] * 2


def test_no_invented_backend(config: ClusterConfig) -> None:
    data = config.model_dump()
    del data["boards"][0]["backend"]
    with pytest.raises(ValidationError):
        ClusterConfig.model_validate(data)


def test_input_paths_relative_to_yaml(tmp_path: Path) -> None:
    config_file = tmp_path / "configs" / "job.yaml"
    config_file.parent.mkdir()
    config_file.write_text("binary: ../build/hello.elf\npriority: 3\n")
    config = JobConfig.from_yaml(config_file, {"priority": -1})
    assert config.binary == str(tmp_path / "build" / "hello.elf")
    assert config.priority == -1


def test_artifact_kind_must_match_slot() -> None:
    ref = ArtifactRef(kind="bitstream", sha256="f" * 64, size_bytes=1)
    with pytest.raises(ValidationError):
        JobSpec.from_config(JobConfig(), "alice", binary=ref)
