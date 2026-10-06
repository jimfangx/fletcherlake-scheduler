"""Real management workflow; only privilege, Pixi and launchd effects are injected."""

import os
import signal
import sys
from pathlib import Path

from fl import ClusterSetup
from fl_agent.configuration import load_config, write_config
from fl_common.files import atomic_write


def main():
    project, state, runtime = map(Path, sys.argv[1:4])
    boundary, agent_pid = sys.argv[4], int(sys.argv[5])
    registered = project / "registered"

    def checkpoint(stage):
        if stage == boundary:
            atomic_write(project / "checkpoint", stage.encode())
            while True:
                signal.pause()

    def editor(path):
        checkpoint("editor")
        updated = load_config(state)
        updated.apple_model = "reconfigured Mac"
        write_config(state, updated)

    def runner(argv):
        if argv[1] == "install":
            checkpoint("sync")
        elif argv[1] == "bootout":
            if registered.read_text() != "yes":
                raise RuntimeError("Cannot bootout a service that is not loaded")
            os.kill(agent_pid, signal.SIGTERM)
            atomic_write(registered, b"no")
            checkpoint("bootout")
        else:
            raise AssertionError("Crash boundary was not reached before service bootstrap")

    setup = ClusterSetup(
        state,
        runtime,
        authorize=lambda: None,
        pixi="/opt/pixi/bin/pixi",
        launchd_root=project / "launchd",
        runner=runner,
        probe=lambda _: registered.read_text() == "yes",
    )
    setup.reconfigure(project, editor=editor)


if __name__ == "__main__":
    main()
