"""Hand enrollment and agent confirmation to launchd before switching Tailscale networks."""

import argparse
import getpass
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fl import Cluster, ClusterSetup
from fl.macos.inventory import inventory
from fl_agent.configuration import DEFAULT_STATE_ROOT, config_confirmed, load_config
from fl_agent.credentials import load_credentials
from fl_common.errors import PlatformError
from fl_common.files import atomic_write
from fl_common.models import ClusterConfig
from fl_common.models.base import utcnow
from fl_common.network import https_origin

PROJECT = Path("/opt/fl")
STATE = DEFAULT_STATE_ROOT
LABEL = "edu.berkeley.fletcherlake.enrollment"
REQUEST = "enrollment-request.json"
PROGRESS = "enrollment-progress.json"


def progress(phase, **details):
    record = {"phase": phase, "timestamp": utcnow().isoformat(), **details}
    atomic_write(STATE / PROGRESS, json.dumps(record, indent=2).encode())
    print(json.dumps(record), flush=True)


def request():
    path = STATE / REQUEST
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError("Enrollment request must be owned by root and mode 0600")
    return json.loads(path.read_text())


def completed():
    if (STATE / REQUEST).exists() or not (STATE / "credentials.json").exists():
        return False
    path = STATE / PROGRESS
    return path.exists() and json.loads(path.read_text()).get("phase") == "COMPLETE"


def launch(config=None, scheduler=None, *, resume=False):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if resume:
        if completed():
            print("Enrollment already completed; the agent's launchd job manages its connection.")
            return
        request()
    else:
        resolved = inventory(config)
        origin = https_origin(scheduler)
        token = getpass.getpass("Enrollment token: ")
        if not 32 <= len(token) <= 256:
            raise RuntimeError("Enrollment token must contain 32 to 256 characters")
        document = {"config": resolved.model_dump(mode="json"), "scheduler": origin, "token": token}
        atomic_write(STATE / REQUEST, json.dumps(document).encode())
    logs = STATE / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    definition = {
        "Label": LABEL,
        "ProgramArguments": [
            str(PROJECT / ".pixi/envs/default/bin/python"),
            str(PROJECT / "ship/enroll-mac-background.py"),
            "--worker",
        ],
        "WorkingDirectory": str(PROJECT),
        "EnvironmentVariables": {
            "PATH": "/opt/fl-tools:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        },
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(logs / "enrollment.stdout.log"),
        "StandardErrorPath": str(logs / "enrollment.stderr.log"),
    }
    present = subprocess.run(
        ["/bin/launchctl", "print", "system/" + LABEL], capture_output=True, check=False
    )
    if present.returncode == 0:
        subprocess.run(["/bin/launchctl", "bootout", "system/" + LABEL], check=True)
    path = Path("/Library/LaunchDaemons") / (LABEL + ".plist")
    atomic_write(path, plistlib.dumps(definition), mode=0o644)
    progress("QUEUED")
    print("Enrollment will continue under launchd if SSH disconnects.", flush=True)
    print(f"Status on this Mac: sudo cat '{STATE / PROGRESS}'", flush=True)
    subprocess.run(["/bin/launchctl", "bootstrap", "system", str(path)], check=True)


def ready():
    if not config_confirmed(STATE):
        return False
    try:
        with Cluster.local() as client:
            return client.status()["state"] == "READY"
    except httpx.HTTPError:
        return False


def worker():
    # Let the foreground installer finish printing and close its SSH session first.
    time.sleep(3)
    if completed():
        print("Enrollment already completed; no further work needed.", flush=True)
        return
    document = request()
    config = ClusterConfig.model_validate(document["config"])
    setup = ClusterSetup(pixi="/usr/local/bin/pixi")
    deadline = time.monotonic() + 600
    retryable = {
        "NETWORK_JOIN_FAILED",
        "NETWORK_UNAVAILABLE",
        "NODE_NOT_JOINED",
        "ENROLLMENT_PENDING",
        "AGENT_NOT_READY",
        "SCHEDULER_CONNECTING",
    }
    while True:
        try:
            credentials = load_credentials(STATE / "credentials.json")
            if credentials is None:
                progress("ENROLLING")
                setup.init(
                    config, scheduler=document["scheduler"], enrollment_token=document["token"]
                )
            else:
                if credentials.enrollment_url != document["scheduler"]:
                    raise RuntimeError("Saved credentials belong to a different scheduler")
            if not ready():
                progress("CONFIRMING")
                setup.confirm(PROJECT)
            with Cluster.local() as client:
                snapshot = client.status()
            if not snapshot["system"]["scheduler_connected"]:
                raise PlatformError(
                    "SCHEDULER_CONNECTING", "Waiting for the agent's scheduler connection"
                )
            cluster_id = load_config(STATE).cluster_id
            if cluster_id is None:
                raise RuntimeError("Registered cluster UUID is missing")
            progress(
                "COMPLETE",
                cluster_id=str(cluster_id),
                board_errors=sum(board["state"] == "ERROR" for board in snapshot["boards"]),
            )
            (STATE / REQUEST).unlink()
            return
        except (PlatformError, httpx.HTTPError) as error:
            code = error.code if isinstance(error, PlatformError) else type(error).__name__
            message = (
                error.message
                if isinstance(error, PlatformError)
                else "Scheduler HTTP request failed"
            )
            retry = time.monotonic() < deadline and (
                not isinstance(error, PlatformError) or code in retryable
            )
            progress("RETRYING" if retry else "FAILED", error_code=code, message=message)
            if not retry:
                raise SystemExit(1) from None
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--scheduler")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--worker", action="store_true")
    modes.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin" or os.geteuid() != 0:
        parser.error("Run as root on the target Mac")
    os.umask(0o077)
    if not (args.worker or args.resume) and (not args.config or not args.scheduler):
        parser.error("Provide --config and --scheduler")
    try:
        if args.worker:
            worker()
        else:
            launch(args.config, args.scheduler, resume=args.resume)
    except Exception as error:
        code = error.code if isinstance(error, PlatformError) else type(error).__name__
        # Provider exceptions can contain credentials; record only their type here.
        progress("FAILED", error_code=code)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
