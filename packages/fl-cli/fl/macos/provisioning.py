"""Local provisioning primitives, with an injectable runner for macOS contract tests."""

import os
import platform
import plistlib
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from fl_agent.configuration import confirm_config, invalidate_config, load_config
from fl_agent.files import atomic_write
from fl_common.errors import PlatformError

Runner = Callable[[list[str]], None]
Probe = Callable[[str], bool]
LABEL = "edu.berkeley.fletcherlake.agent"


def run(argv: list[str]) -> None:
    subprocess.run(argv, check=True, timeout=600)


def loaded(label: str) -> bool:
    """Inspect registration separately from whether its plist is installed on disk."""
    result = subprocess.run(
        ["/bin/launchctl", "print", f"system/{label}"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode not in {0, 113}:
        raise PlatformError(
            "LAUNCHD_QUERY_FAILED",
            "Cannot inspect launchd registration",
            exit_code=result.returncode,
        )
    return result.returncode == 0


def require_macos() -> None:
    if platform.system() != "Darwin":
        raise PlatformError(
            "MACOS_REQUIRED", "This lifecycle operation must be run on the Mac Mini"
        )


def launchd_plist(project: Path, state_root: Path, runtime_root: Path, pixi: str) -> bytes:
    logs = state_root / "logs"
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [
                pixi,
                "run",
                "--manifest-path",
                str(project / "pixi.toml"),
                "fl-agent",
                "--state-root",
                str(state_root),
                "--runtime-root",
                str(runtime_root),
            ],
            "WorkingDirectory": str(project),
            "EnvironmentVariables": {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "ThrottleInterval": 10,
            "Umask": 0o077,
            "StandardOutPath": str(logs / "agent.stdout.log"),
            "StandardErrorPath": str(logs / "agent.stderr.log"),
        }
    )


def install_agent(
    project: Path,
    state_root: Path,
    runtime_root: Path,
    *,
    runner: Runner = run,
    launchd_root: Path = Path("/Library/LaunchDaemons"),
    pixi: str | None = None,
    installed: bool = False,
    probe: Probe = loaded,
) -> None:
    load_config(state_root)
    executable = pixi or shutil.which("pixi")
    if not executable:
        raise PlatformError("PIXI_MISSING", "Install Pixi before confirming the cluster")
    if not (project / "pixi.toml").is_file():
        raise PlatformError("PROJECT_MISSING", "Project path must contain pixi.toml")
    # Installation failure leaves the SHA absent, so a later boot cannot run jobs.
    invalidate_config(state_root)
    runner([executable, "install", "--locked", "--manifest-path", str(project / "pixi.toml")])
    state_root.chmod(0o700)
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_root.chmod(0o700)
    (state_root / "logs").mkdir(exist_ok=True)
    plist = launchd_root / f"{LABEL}.plist"
    atomic_write(plist, launchd_plist(project, state_root, runtime_root, executable), mode=0o644)
    confirm_config(state_root)
    if installed and probe(LABEL):
        runner(["/bin/launchctl", "bootout", f"system/{LABEL}"])
    runner(["/bin/launchctl", "bootstrap", "system", str(plist)])
    runner(["/bin/launchctl", "kickstart", "-k", f"system/{LABEL}"])


def require_admin() -> None:
    """Library callers must obtain privilege themselves; never re-execute their script."""
    require_macos()
    if os.geteuid() != 0:
        raise PlatformError(
            "ROOT_REQUIRED", "Run macOS lifecycle operations with administrator privilege"
        )
