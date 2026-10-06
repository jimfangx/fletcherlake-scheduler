"""Forced SSH command that replaces untrusted BBCP options and filesystem arguments."""

import argparse
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models.artifact import ArtifactKind
from fl_common.models.base import utcnow
from fl_common.models.transfer import TransferGrant

from .ports import reserve
from .store import GatewayStore

# Receiver options come from stdin, not SSH_ORIGINAL_COMMAND. Unknown/dangerous
# switches (programs, recursion, config files, logging, symlinks) never reach BBCP.
FLAGS = {"-k", "-n", "-o", "-f", "-z"}
VALUES = {"-b", "-m", "-s", "-t", "-W", "-U", "-Y", "-Z", "-H", "-N"}


def request(stream: BinaryIO, grant: TransferGrant, original: str) -> tuple[str, ArtifactKind]:
    expected = "SNK" if grant.direction == "upload" else "SRC"
    if original != f"bbcp {expected}":
        raise PlatformError("TRANSFER_PROTOCOL", "Only a scoped BBCP operation is permitted")
    first = stream.readline(4097)
    if len(first) > 4096 or not first.endswith(b"\n"):
        raise PlatformError("TRANSFER_PROTOCOL", "Invalid BBCP configuration record")
    tokens = first.decode("ascii").split()
    options: dict[str, str] = {}
    while tokens:
        name = tokens.pop(0)
        if name in options or name not in FLAGS | VALUES:
            raise PlatformError("TRANSFER_PROTOCOL", "Unsupported BBCP option")
        if name in VALUES:
            if not tokens or tokens[0].startswith("-"):
                raise PlatformError("TRANSFER_PROTOCOL", "Invalid BBCP option value")
            options[name] = tokens.pop(0)
        else:
            options[name] = ""
    mode = "o" if grant.direction == "upload" else "i"
    if (
        not re.fullmatch(r"[0-9a-f]{64}", options.get("-Y", ""))
        or options.get("-N") != mode
        or options.get("-H") != "none:0"
        or ("-z" in options) != (grant.direction == "download")
    ):
        raise PlatformError("TRANSFER_PROTOCOL", "Unsupported BBCP stream configuration")
    second = stream.readline(257)
    if len(second) > 256 or not second.endswith(b"\n") or stream.read(1) != b"\0":
        raise PlatformError("TRANSFER_PROTOCOL", "Expected one fixed artifact path")
    path = second.decode("ascii").rstrip("\n").split("/")
    allowed = {ref.kind for ref in grant.files}
    if (
        len(path) != 4
        or path[:3] != ["", "transfer", str(grant.transfer_id)]
        or path[3] not in allowed
    ):
        raise PlatformError("TRANSFER_SCOPE", "Artifact path is outside this grant")
    kind: ArtifactKind = next(ref.kind for ref in grant.files if ref.kind == path[3])
    return options["-Y"], kind


def deadline(signum: int, frame: object) -> None:
    raise TimeoutError("Transfer deadline elapsed")


def run_bbcp(executable: Path, role: str, data: bytes, environment: dict[str, str]) -> None:
    process = subprocess.Popen(
        [str(executable), role],
        stdin=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    try:
        process.communicate(input=data)
        if process.returncode:
            raise PlatformError("TRANSFER_FAILED", "BBCP stream did not complete")
    finally:
        # Reap all helpers before the caller releases its reserved listener port.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def serve(store: GatewayStore, transfer_id: UUID) -> None:
    grant, state = store.lookup(transfer_id)
    if grant.direction == "upload" and state != "OPEN":
        raise PlatformError("TRANSFER_SCOPE", "Verified uploads are immutable")
    remaining = max(1, math.ceil((grant.expires_at - utcnow()).total_seconds()))
    port_deadline = time.monotonic() + remaining
    signal.signal(signal.SIGALRM, deadline)
    signal.signal(signal.SIGTERM, deadline)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        token, kind = request(sys.stdin.buffer, grant, os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        # BBCP tokenizes program pipes by spaces. An interpreter alias avoids imposing
        # that constraint on the installation path; root and executable paths use env.
        with (
            reserve(
                store.root, store.data_port_first, store.data_port_last, deadline=port_deadline
            ) as port,
            tempfile.TemporaryDirectory(prefix="fl-pipe-", dir="/tmp") as name,
        ):
            interpreter = Path(name) / "python"
            interpreter.symlink_to(sys.executable)
            operation = "receive" if grant.direction == "upload" else "send"
            mode = "o" if grant.direction == "upload" else "i"
            ports = str(port)
            flags = f"-n -N {mode} -o -s 4 -W 131072 -Y {token} -Z {ports} -H none:0" + (
                " -z" if grant.direction == "download" else ""
            )
            program = (
                f"{interpreter} -I -m fl_gateway.stream {operation} "
                f"--transfer {transfer_id} --kind {kind}"
            )
            data = (flags + "\n" + program + "\n\0").encode()
            environment = {
                "PATH": "/usr/bin:/bin",
                "BBCP_ALLOWPP": str(interpreter),
                "FL_GATEWAY_ROOT": str(store.root),
                "FL_GATEWAY_BBCP": str(store.bbcp),
            }
            run_bbcp(store.bbcp, "SNK" if grant.direction == "upload" else "SRC", data, environment)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--bbcp", required=True, type=Path)
    parser.add_argument("--transfer", required=True, type=UUID)
    parser.add_argument("--data-port-first", type=int, default=5000)
    parser.add_argument("--data-port-last", type=int, default=5099)
    args = parser.parse_args()
    try:
        serve(
            GatewayStore(
                args.root,
                args.bbcp,
                data_port_first=args.data_port_first,
                data_port_last=args.data_port_last,
            ),
            args.transfer,
        )
    except (PlatformError, OSError, ValueError, TimeoutError) as error:
        code = error.code if isinstance(error, PlatformError) else type(error).__name__
        print(f"Gateway transfer rejected ({code})", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
