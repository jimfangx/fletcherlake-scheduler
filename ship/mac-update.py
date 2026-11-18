"""Activate a prepared Mac release using the agent's protected reconfiguration workflow."""

import json
import os
import shutil
import sys
from pathlib import Path

from fl import ClusterSetup
from fl.macos.launchd import Installation
from fl.macos.provisioning import LABEL
from fl.macos.retirement import stopped_agent
from fl_agent.configuration import load_config


def update(release: Path, project: Path = Path("/opt/fl"), *, setup=None) -> dict:
    setup = setup or ClusterSetup(pixi="/usr/local/bin/pixi")
    setup.authorize()
    release = release.resolve(strict=True)
    previous = project.resolve(strict=True)
    plist = setup.launchd_root / f"{LABEL}.plist"
    plan = Installation.from_agent(plist, setup.state_root, setup.runtime_root)
    if plan.project.resolve() != previous:
        raise RuntimeError("The installed agent must use /opt/fl before running this update")
    if release == previous or not (release / "pixi.toml").is_file():
        raise RuntimeError("Prepare a separate release before activation")
    if not (setup.state_root / "credentials.json").is_file():
        raise RuntimeError("This Mac is not enrolled; use ship/18-bringup-mac.sh")

    def activate(config: Path) -> None:
        # reconfigure holds the management lock and has already drained the old daemon.
        if project.resolve() != previous:
            raise RuntimeError("The active release changed during preparation; retry the update")
        if not setup.probe(LABEL):
            raise RuntimeError("The installed agent must be running before updating")
        # Validate the existing definition before stopping the daemon or changing /opt/fl.
        load_config(setup.state_root)
        setup.runner(["/bin/launchctl", "bootout", f"system/{LABEL}"])
        with stopped_agent(setup.state_root, setup.runtime_root):
            backup = release / "previous-installation"
            backup.mkdir(mode=0o700)
            shutil.copy2(config, backup / "cluster.yaml")
            shutil.copy2(plist, backup / plist.name)
            link = release / "active-link"
            link.symlink_to(release, target_is_directory=True)
            if project.is_symlink():
                (backup / "previous-project").write_text(str(previous) + "\n")
                os.replace(link, project)
            else:
                # Preserve the original directory, including its environment and native tools.
                legacy = release.parent / f"legacy-{release.name}"
                if legacy.exists():
                    raise RuntimeError(f"Previous release backup already exists: {legacy}")
                (backup / "previous-project").write_text(str(legacy) + "\n")
                project.rename(legacy)
                try:
                    os.replace(link, project)
                except BaseException:
                    legacy.rename(project)
                    raise

    # This invalidates confirmation before drain, preserves UUID/config/queue/credentials,
    # installs the launchd definition for the new release, and waits for local readiness.
    return setup.reconfigure(project, editor=activate)


if __name__ == "__main__":
    print(json.dumps(update(Path(sys.argv[1])), indent=2))
