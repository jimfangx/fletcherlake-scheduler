"""Die after files disappear but before the durable deletion transaction completes."""

import os
import shutil
import signal
import sys
from pathlib import Path

from fl_agent.collateral import CollateralStore
from fl_agent.db import AgentDB

if __name__ == "__main__":
    root = Path(sys.argv[1])
    db = AgentDB(root / "agent.db")
    store = CollateralStore(root / "jobs", db)
    actual_remove = shutil.rmtree

    def crash_after_remove(path: Path) -> None:
        actual_remove(path)
        os.kill(os.getpid(), signal.SIGKILL)

    shutil.rmtree = crash_after_remove
    store.sweep()
