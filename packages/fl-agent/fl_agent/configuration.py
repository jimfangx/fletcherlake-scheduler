"""The SHA file is a persistent confirmation marker, not a substitute for validation."""

import hashlib
from pathlib import Path

import yaml
from fl_common.models import ClusterConfig

from .files import atomic_write, fsync_directory

DEFAULT_STATE_ROOT = Path("/Library/Application Support/fl")
DEFAULT_RUNTIME_ROOT = Path("/var/run/fl")


def load_config(root: Path) -> ClusterConfig:
    return ClusterConfig.model_validate(yaml.safe_load((root / "cluster.yaml").read_text()))


def config_confirmed(root: Path) -> bool:
    config = root / "cluster.yaml"
    marker = root / "cluster.sha256"
    if not config.is_file() or not marker.is_file():
        return False
    return marker.read_text().strip() == hashlib.sha256(config.read_bytes()).hexdigest()


def write_config(root: Path, config: ClusterConfig) -> None:
    root.mkdir(parents=True, exist_ok=True)
    invalidate_config(root)
    text = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    atomic_write(root / "cluster.yaml", text.encode())


def invalidate_config(root: Path) -> None:
    """Persist the missing confirmation marker before network, drain, or editor operations."""
    (root / "cluster.sha256").unlink(missing_ok=True)
    fsync_directory(root)


def confirm_config(root: Path) -> None:
    load_config(root)
    config = root / "cluster.yaml"
    config.chmod(0o600)
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    atomic_write(root / "cluster.sha256", (digest + "\n").encode())
