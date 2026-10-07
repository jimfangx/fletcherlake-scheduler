"""Per-key forced SFTP subsystem; SSH provides encryption, identity and source fencing."""

import argparse
import os
import signal
import sys
from pathlib import Path
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models.base import utcnow

from .sftp import SFTP
from .store import GatewayStore


def request(original: str) -> None:
    if original != "internal-sftp":
        raise PlatformError("TRANSFER_PROTOCOL", "Only the scoped SFTP subsystem is permitted")


def deadline(signum: int, frame: object) -> None:
    raise TimeoutError("Transfer deadline elapsed")


def serve(store: GatewayStore, transfer_id: UUID) -> None:
    request(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    grant, state = store.lookup(transfer_id)
    if grant.direction == "upload" and state != "OPEN":
        raise PlatformError("TRANSFER_SCOPE", "Verified uploads are immutable")
    remaining = max(0.001, (grant.expires_at - utcnow()).total_seconds())
    signal.signal(signal.SIGALRM, deadline)
    signal.signal(signal.SIGTERM, deadline)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        with SFTP(store, grant) as server:
            server.serve(sys.stdin.buffer, sys.stdout.buffer)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--transfer", required=True, type=UUID)
    args = parser.parse_args()
    try:
        serve(GatewayStore(args.root), args.transfer)
    except (PlatformError, OSError, ValueError, TimeoutError) as error:
        code = error.code if isinstance(error, PlatformError) else type(error).__name__
        print(f"Gateway transfer rejected ({code})", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
