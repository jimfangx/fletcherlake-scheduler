"""Create a fresh protected review bundle, never edit installed configurations."""

import shutil
from pathlib import Path

import yaml
from fl_common.files import atomic_write

from .control import render_control
from .manifest import Deployment
from .proxies import render_proxies
from .settings import render_settings
from .units import render_units


def load(path: Path) -> Deployment:
    return Deployment.model_validate(yaml.safe_load(path.read_text()))


def render(manifest: Deployment, templates: Path) -> dict[str, str]:
    return {
        **render_control(manifest, templates),
        **render_proxies(manifest, templates),
        **render_units(manifest, templates),
        **render_settings(manifest),
        "manifest.yaml": yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False),
    }


def write_bundle(files: dict[str, str], output: Path) -> None:
    # Refuse an existing destination, including symlinks and empty directories.
    output.mkdir(mode=0o700)
    try:
        for name, source in files.items():
            path = output / name
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            atomic_write(path, source.encode())
    except BaseException:
        shutil.rmtree(output)
        raise
