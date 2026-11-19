"""Destructively remove the standard local Mac installation before fresh enrollment."""

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

LABELS = (
    "edu.berkeley.fletcherlake.enrollment",
    "edu.berkeley.fletcherlake.agent",
    "edu.berkeley.fletcherlake.retention",
)
ROOTS = (
    Path("/opt/fl"),
    Path("/opt/fl-releases"),
    Path("/opt/fl-tools"),
    Path("/Library/Application Support/fl"),
    Path("/Library/Application Support/fl-retired"),
    Path("/var/run/fl"),
)


def processes():
    output = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,stat=,command="], text=True)
    return {
        int(pid): (int(parent), status, command)
        for line in output.splitlines()
        for pid, parent, status, command in [line.strip().split(None, 3)]
    }


def owned_processes(snapshot, known=()):
    owned = set(known) | {
        pid
        for pid, (_, _, command) in snapshot.items()
        if re.search(r"(?:^|\s)/opt/(?:fl/|fl-releases/)", command)
    }
    while True:
        children = {pid for pid, (parent, _, _) in snapshot.items() if parent in owned}
        if children.issubset(owned):
            return owned - {os.getpid()}
        owned.update(children)


def send_signal(snapshot, targets, sig):
    current = processes()
    for pid in targets:
        # Check the command again to avoid signaling a PID reused during shutdown.
        if pid in snapshot and pid in current and snapshot[pid][2] == current[pid][2]:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass


def reset(roots=ROOTS, launchd=Path("/Library/LaunchDaemons")):
    print(
        "Destructive redeployment: stopping the old agent and removing local releases and state.",
        flush=True,
    )
    snapshot = processes()
    targets = owned_processes(snapshot)
    for label in LABELS:
        job = "system/" + label
        found = subprocess.run(["/bin/launchctl", "print", job], capture_output=True, check=False)
        if found.returncode == 0:
            result = subprocess.run(
                ["/bin/launchctl", "bootout", job], capture_output=True, timeout=90, check=False
            )
            if result.returncode:
                raise RuntimeError("Could not unload " + label)
    # launchd stops the agent gracefully, allowing its shutdown handlers to stop/power off
    # boards. Detached lilikoi daemons and leftover native subprocesses must also stop.
    remaining = processes()
    targets = owned_processes(remaining, targets)
    snapshot.update(remaining)
    send_signal(snapshot, targets, signal.SIGTERM)
    alive = targets
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        remaining = processes()
        alive = {
            pid for pid in targets if pid in remaining and not remaining[pid][1].startswith("Z")
        }
        if not alive:
            break
        time.sleep(0.1)
    else:
        send_signal(snapshot, alive, signal.SIGKILL)
        time.sleep(0.2)
    remaining = processes()
    if any(pid in remaining and not remaining[pid][1].startswith("Z") for pid in targets):
        raise RuntimeError(
            "An old agent/helper process is still running; local files were not removed"
        )
    for label in LABELS:
        (launchd / (label + ".plist")).unlink(missing_ok=True)
    for root in roots:
        if root.is_symlink() or root.is_file():
            root.unlink()
        elif root.exists():
            # Fixed application roots only; never follow /opt/fl's release symlink.
            shutil.rmtree(root)
        print("Removed old local installation path:", root, flush=True)


if __name__ == "__main__":
    if sys.platform != "darwin" or os.geteuid() != 0:
        raise SystemExit("Run this reset helper as root on the target Mac")
    reset()
