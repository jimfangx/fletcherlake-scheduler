"""Die around the atomic archival boundary, leaving an already-fsynced retirement intent."""

import os
import signal
import sys
from pathlib import Path

from fl_agent.retirement import Retirement, archive_state
from fl_common.locks import ExclusiveLock

if __name__ == "__main__":
    root = Path(sys.argv[1])
    receipt = Retirement.load(root)
    assert receipt is not None
    with (
        ExclusiveLock(root / "management.lock"),
        ExclusiveLock(receipt.state_root / "agent.lock"),
        ExclusiveLock(receipt.runtime_root / "agent.lock"),
    ):
        if sys.argv[2] == "after":
            archive_state(root, receipt)
        os.kill(os.getpid(), signal.SIGKILL)
