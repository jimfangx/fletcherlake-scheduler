"""Install the SSH relay's nginx route and narrow Headscale ACL on an existing scheduler."""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

SSH_LOCATION = re.compile(r"^    location /api/ssh/ \{\n.*?^    \}", re.MULTILINE | re.DOTALL)
PUBLIC_ANCHOR = "    location ^~ /api/agents/ { return 404; }"


def proxy_config(installed: str, template: str) -> str:
    blocks = SSH_LOCATION.findall(template)
    if len(blocks) != 1 or installed.count(PUBLIC_ANCHOR) != 1:
        raise ValueError("Cannot identify the scheduler public nginx server safely")
    if "location /api/ssh/" in installed:
        if len(SSH_LOCATION.findall(installed)) != 1:
            raise ValueError("Unrecognized existing SSH proxy configuration")
        return SSH_LOCATION.sub(lambda match: blocks[0], installed)
    return installed.replace(PUBLIC_ANCHOR, PUBLIC_ANCHOR + "\n" + blocks[0], 1)


def policy_config(installed: str) -> str:
    policy = json.loads(installed)
    rules = policy["acls"]
    for rule in rules:
        if (
            rule.get("action") == "accept"
            and rule.get("proto") == "tcp"
            and rule.get("src") == ["tag:scheduler"]
        ):
            if "tag:cluster:22" in rule["dst"]:
                return installed
            rule["dst"].append("tag:cluster:22")
            break
    else:
        rules.append(
            {
                "action": "accept",
                "proto": "tcp",
                "src": ["tag:scheduler"],
                "dst": ["tag:cluster:22"],
            }
        )
    return json.dumps(policy, indent=2) + "\n"


def replace(path: Path, content: bytes) -> None:
    info = path.stat()
    descriptor, temporary = tempfile.mkstemp(prefix=".fl-ssh-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), info.st_mode & 0o777)
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run(*argv: str) -> None:
    subprocess.run(argv, check=True, timeout=30)


def reload_services() -> None:
    # The shipped Headscale unit has no ExecReload; SIGHUP reloads its policy in place.
    run("systemctl", "kill", "--kill-whom=main", "--signal=HUP", "headscale")
    run("systemctl", "reload", "nginx")


def install(
    source: Path,
    nginx: Path = Path("/etc/nginx/conf.d/10-fl-scheduler.conf"),
    policy: Path = Path("/etc/headscale/policy.json"),
    backups: Path = Path("/var/backups/fl-ssh-relay"),
) -> None:
    for path in (nginx, policy):
        if path.is_symlink() or not path.is_file():
            raise ValueError("Expected a regular installed configuration file: " + str(path))
    original = {path: path.read_bytes() for path in (nginx, policy)}
    updated = {
        nginx: proxy_config(
            original[nginx].decode(), (source / "services/scheduler/nginx.conf").read_text()
        ).encode(),
        policy: policy_config(original[policy].decode()).encode(),
    }
    if updated == original:
        print("Scheduler SSH proxy and policy are already installed")
        return
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(
        tempfile.mkdtemp(prefix=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-"), dir=backups)
    )
    for path in original:
        shutil.copy2(path, backup / path.name)
    reloaded = False
    try:
        for path, content in updated.items():
            replace(path, content)
        run("nginx", "-t")
        run(
            "headscale",
            "--config",
            "/etc/headscale/config.yaml",
            "policy",
            "check",
            "-f",
            str(policy),
        )
        reloaded = True
        reload_services()
    except BaseException:
        for path, content in original.items():
            replace(path, content)
        if reloaded:
            reload_services()
        raise
    print("Scheduler SSH proxy and policy installed; backup: " + str(backup))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="?", default=Path("/opt/fl"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root on the scheduler VM")
    install(args.source)


if __name__ == "__main__":
    main()
