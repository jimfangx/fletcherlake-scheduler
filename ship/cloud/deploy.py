"""Coordinate one tested release on the existing scheduler and gateway via GCP IAP."""

import argparse
import hashlib
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Target:
    role: str
    instance: str
    port: int


def run(argv: list[str]) -> None:
    subprocess.run(argv, stdin=subprocess.DEVNULL, check=True)


def ssh_options(target: Target, project: str, zone: str, key: Path, hosts: Path) -> list[str]:
    proxy = shlex.join(
        [
            "gcloud",
            "compute",
            "start-iap-tunnel",
            target.instance,
            str(target.port),
            "--listen-on-stdin",
            f"--project={project}",
            f"--zone={zone}",
            "--verbosity=error",
            "--quiet",
        ]
    )
    return [
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={hosts}",
        "-o",
        f"HostKeyAlias=fl-{target.role}-ci",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=10",
        "-o",
        f"ProxyCommand={proxy}",
    ]


def release_id(revision: str) -> str:
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    if (
        not re.fullmatch(r"[a-f0-9]{40}", revision)
        or not run_id.isdecimal()
        or not attempt.isdecimal()
    ):
        raise ValueError("Invalid release identity")
    return f"{revision}-{run_id}-{attempt}"


def deploy(archive: Path, revision: str) -> None:
    identity = release_id(revision)
    project, zone = os.environ["FL_GCP_PROJECT"], os.environ["FL_GCP_ZONE"]
    targets = [
        Target("scheduler", os.environ["FL_SCHEDULER_VM"], 22),
        Target("gateway", os.environ["FL_GATEWAY_VM"], 2222),
    ]
    with archive.open("rb") as archive_file:
        digest = hashlib.file_digest(archive_file, "sha256").hexdigest()
    with tempfile.TemporaryDirectory(prefix="fl-deploy-") as temporary:
        root = Path(temporary)
        key, hosts = root / "identity", root / "known_hosts"
        key.write_text(os.environ.pop("FL_DEPLOY_SSH_KEY").strip() + "\n")
        key.chmod(0o600)
        hosts.write_text(os.environ["FL_DEPLOY_KNOWN_HOSTS"])
        options = {t.role: ssh_options(t, project, zone, key, hosts) for t in targets}

        def remote(target: Target, phase: str) -> None:
            print(f"{target.role}: {phase}", flush=True)
            command = shlex.join(
                [
                    "sudo",
                    "-n",
                    "bash",
                    "/home/fl-deploy/cloud-update.sh",
                    phase,
                    target.role,
                    identity,
                    revision,
                    digest,
                ]
            )
            run(["ssh", *options[target.role], f"fl-deploy@{target.instance}", command])

        for target in targets:
            for source, name in (
                (archive, "cloud-release.tar.gz"),
                (Path(__file__).with_name("update-host.sh"), "cloud-update.sh"),
            ):
                run(
                    [
                        "scp",
                        *options[target.role],
                        str(source),
                        f"fl-deploy@{target.instance}:{name}",
                    ]
                )
            remote(target, "prepare")
        # Preparation failures leave both existing services running. From this point,
        # migrations may change state: on failure stop both and retain backups for recovery.
        try:
            for phase in ("stop", "backup", "activate"):
                for target in targets:
                    remote(target, phase)
            remote(targets[0], "migrate")
            remote(targets[1], "start")
            remote(targets[0], "start")
            run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "30",
                    f"https://scheduler.{os.environ['FL_DOMAIN']}/healthz",
                ]
            )
        except BaseException:
            for target in targets:
                try:
                    remote(target, "stop")
                except (OSError, subprocess.CalledProcessError):
                    print(f"Could not confirm {target.role} stopped; inspect that VM", flush=True)
            print(
                f"Release failed. Backups on each VM: /var/backups/fl-cloud/{identity}", flush=True
            )
            raise
    print(f"Both cloud services deployed at {revision}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    deploy(args.archive, args.revision)


if __name__ == "__main__":
    main()
