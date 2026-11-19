"""Rotate the scheduler's Headscale credential through the local administrative socket."""

import argparse
import fcntl
import ipaddress
import json
import os
import shlex
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENV_FILE = Path("/etc/fl/scheduler.env")
STATE = Path("/var/lib/fl-headscale-key-rotation")
SETTING = "FL_HEADSCALE_API_KEY"


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        # Headscale's output can contain credentials. Never include it in the journal.
        raise RuntimeError("Administrative command failed: " + args[0])
    return result.stdout


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_check(url, key):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or not ipaddress.ip_address(parsed.hostname).is_loopback
        or parsed.username
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Headscale administrative URL must be loopback HTTP")
    request = Request(url.rstrip("/") + "/api/v1/node", headers={"Authorization": "Bearer " + key})
    with build_opener(NoRedirect()).open(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("Headscale credential verification failed")


def loaded_environment():
    pid = command("systemctl", "show", "fl-scheduler", "-p", "MainPID", "--value").strip()
    if not pid.isdecimal() or int(pid) == 0:
        raise RuntimeError("Scheduler is not running")
    return dict(
        item.decode().split("=", 1)
        for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if b"=" in item
    )


def atomic_write(path, content, owner):
    fd, name = tempfile.mkstemp(prefix=".headscale-rotation-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def key_metadata(rows, key):
    # Headscale 0.29's JSON contains display prefixes such as hskey-api-<prefix>-***.
    matches = [row for row in rows if key.startswith(row["prefix"].removesuffix("***"))]
    if len(matches) != 1:
        raise RuntimeError("Cannot identify the scheduler's Headscale API key")
    return matches[0]


def restart_and_check(key, url):
    command("systemctl", "restart", "fl-scheduler")
    for _ in range(45):
        try:
            if loaded_environment().get(SETTING) != key:
                raise RuntimeError("Scheduler did not load replacement credential")
            with build_opener(NoRedirect()).open("http://127.0.0.1:8080/healthz", timeout=2):
                pass
            api_check(url, key)
            return
        except (OSError, RuntimeError):
            time.sleep(1)
    raise RuntimeError("Scheduler did not become healthy after credential rotation")


def rotate(headscale, *, force=False, check=False):
    if command("systemctl", "is-active", "fl-scheduler").strip() != "active":
        raise RuntimeError("Scheduler is not active")
    env = loaded_environment()
    current, url = env[SETTING], env["FL_HEADSCALE_ADMIN_URL"]
    owner = ENV_FILE.lstat()
    if not stat.S_ISREG(owner.st_mode) or owner.st_uid != 0 or owner.st_mode & 0o077:
        raise RuntimeError("Scheduler environment must be a protected root-owned regular file")
    original = ENV_FILE.read_text()
    lines = original.splitlines(keepends=True)
    positions = [i for i, line in enumerate(lines) if line.startswith(SETTING + "=")]
    if len(positions) != 1:
        raise RuntimeError("Expected exactly one scheduler Headscale credential setting")
    position = positions[0]
    saved = shlex.split(lines[position].partition("=")[2])
    if len(saved) != 1:
        raise RuntimeError("Invalid scheduler Headscale credential setting")

    def cli(*args):
        return command(
            "/usr/sbin/runuser",
            "-u",
            "headscale",
            "--",
            headscale,
            "--config",
            "/etc/headscale/config.yaml",
            "apikeys",
            *args,
        )

    rows = json.loads(cli("list", "--output", "json"))
    old = key_metadata(rows, current)
    expiration = old.get("expiration")
    due = expiration is None or int(expiration["seconds"]) <= time.time() + 7 * 86400
    if check:
        api_check(url, current)
        print(
            f"Headscale credential accepted; rotation due: {due}; "
            f"pending reload: {saved[0] != current}"
        )
        return
    if saved[0] != current:
        # Recover a crash after replacing the file but before restarting the scheduler.
        replacement = saved[0]
        key_metadata(rows, replacement)
        api_check(url, replacement)
    elif force or due:
        replacement = json.loads(cli("create", "--expiration", "720h", "--output", "json"))
        if (
            not isinstance(replacement, str)
            or not replacement
            or any(c.isspace() for c in replacement)
        ):
            raise RuntimeError("Headscale returned an invalid replacement credential")
        api_check(url, replacement)
        atomic_write(STATE / "scheduler.env.before-rotation", original, owner)
        lines[position] = SETTING + "=" + json.dumps(replacement) + "\n"
        atomic_write(ENV_FILE, "".join(lines), owner)
    else:
        api_check(url, current)
        print("Headscale credential has more than seven days remaining; no rotation needed")
        return
    try:
        restart_and_check(replacement, url)
    except Exception:
        # Keep the old key valid. A failed restart must not remove the recovery credential.
        lines[position] = SETTING + "=" + json.dumps(current) + "\n"
        atomic_write(ENV_FILE, "".join(lines), owner)
        command("systemctl", "restart", "fl-scheduler")
        raise RuntimeError("Rotation failed; restored previous credential") from None
    cli("expire", "--id", str(int(old["id"])))
    print("Headscale credential rotated; scheduler healthy; previous key expired")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headscale-binary", default="/usr/local/bin/headscale")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--force", action="store_true")
    modes.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root on the scheduler VM")
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / "rotation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            rotate(args.headscale_binary, force=args.force, check=args.check)
        except Exception as error:
            # Provider exceptions may contain secret-bearing output. Print only curated errors.
            message = str(error) if type(error) is RuntimeError else type(error).__name__
            parser.exit(1, "Headscale key rotation failed: " + message + "\n")


if __name__ == "__main__":
    main()
