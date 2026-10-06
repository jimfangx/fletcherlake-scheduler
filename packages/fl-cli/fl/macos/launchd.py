"""Query native service registration and install the independent retention launchd job."""

import os
import plistlib
from pathlib import Path

from fl_agent.retired_state import protected
from fl_agent.retirement import RetiredInstallation
from fl_common.errors import PlatformError
from fl_common.files import atomic_write

from .provisioning import LABEL, Probe, Runner, launchd_plist
from .provisioning import loaded as loaded

RETENTION_LABEL = "edu.berkeley.fletcherlake.retention"


class Installation(RetiredInstallation):
    @classmethod
    def from_agent(cls, plist: Path, state: Path, runtime: Path) -> "Installation":
        info = plist.lstat()
        if plist.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PlatformError("LAUNCHD_CONFIG", "Agent launchd definition must be protected")
        document = plistlib.loads(plist.read_bytes())
        plan = cls(project=document["WorkingDirectory"], pixi=document["ProgramArguments"][0])
        expected = plistlib.loads(launchd_plist(plan.project, state, runtime, plan.pixi))
        actual = list(document["ProgramArguments"])
        # macOS /var/run resolves to /private/var/run; equivalent native roots remain valid.
        for flag, path in (("--state-root", state), ("--runtime-root", runtime)):
            index = actual.index(flag) + 1
            if Path(actual[index]).resolve() == path.resolve():
                actual[index] = str(path)
        if (
            not plan.project.is_absolute()
            or not Path(plan.pixi).is_absolute()
            or (document["Label"] != LABEL or actual != expected["ProgramArguments"])
        ):
            raise PlatformError(
                "LAUNCHD_CONFIG", "Confirm the agent's native launchd definition first"
            )
        return plan

    def retention_plist(self, root: Path) -> bytes:
        document = plistlib.loads(launchd_plist(self.project, root, root, self.pixi))
        document["Label"] = RETENTION_LABEL
        document["ProgramArguments"] = [
            self.pixi,
            "run",
            "--locked",
            "--manifest-path",
            str(self.project / "pixi.toml"),
            "fl-retention",
            "--retired-root",
            str(root),
        ]
        document.pop("KeepAlive")
        document.update(StartInterval=300, ExitTimeOut=30)
        document["StandardOutPath"] = str(root / "logs" / "retention.stdout.log")
        document["StandardErrorPath"] = str(root / "logs" / "retention.stderr.log")
        return plistlib.dumps(document)


def ensure_retention(
    plan: Installation, root: Path, launchd: Path, runner: Runner, probe: Probe
) -> None:
    protected(root, directory=True)
    # Prove the independent entrypoint is runnable before unregister removes network access.
    runner(
        [
            plan.pixi,
            "run",
            "--locked",
            "--manifest-path",
            str(plan.project / "pixi.toml"),
            "fl-retention",
            "--retired-root",
            str(root),
            "--check",
        ]
    )
    logs = root / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    protected(logs, directory=True)
    path = launchd / f"{RETENTION_LABEL}.plist"
    definition = plan.retention_plist(root)
    existing = path.read_bytes() if path.exists() else None
    registered = probe(RETENTION_LABEL)
    if registered and existing == definition:
        return
    if registered:
        runner(["/bin/launchctl", "bootout", f"system/{RETENTION_LABEL}"])
    atomic_write(path, definition, mode=0o644)
    runner(["/bin/launchctl", "bootstrap", "system", str(path)])
