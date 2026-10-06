"""CLI privilege escalation and compatibility exports for shared macOS primitives."""

import os
import sys

from fl.macos.provisioning import LABEL, Runner, install_agent, launchd_plist, require_macos, run

__all__ = [
    "LABEL",
    "Runner",
    "ensure_root",
    "install_agent",
    "launchd_plist",
    "require_macos",
    "run",
]


def ensure_root(runner: Runner = run) -> None:
    """Only CLI handlers re-execute the CLI under sudo."""
    if os.geteuid() != 0:
        runner(["sudo", "-v"])
        runner(["sudo", sys.executable, "-m", "fl_cli", *sys.argv[1:]])
        raise SystemExit(0)
