"""Reserve one configured BBCP listener port across gateway SSH processes."""

import errno
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fl_common.errors import PlatformError
from fl_common.locks import ExclusiveLock


def available(port: int) -> bool:
    """Probe the same dual-stack listener BBCP uses; do not keep its socket open."""
    families = (socket.AF_INET6, socket.AF_INET) if socket.has_ipv6 else (socket.AF_INET,)
    for family in families:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                listener.bind(("::" if family == socket.AF_INET6 else "0.0.0.0", port))
                listener.listen(1)
            return True
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                return False
            if family == socket.AF_INET6 and error.errno in {
                errno.EAFNOSUPPORT,
                errno.EPROTONOSUPPORT,
                errno.EADDRNOTAVAIL,
            }:
                continue
            raise
    return False


@contextmanager
def reserve(root: Path, first: int, last: int, *, deadline: float) -> Iterator[int]:
    """Wait within the caller's transfer deadline; process death releases its lease."""
    while True:
        for port in range(first, last + 1):
            if time.monotonic() >= deadline:
                raise TimeoutError("Transfer deadline elapsed waiting for a BBCP port")
            lock = ExclusiveLock(root / "locks" / f"data-port-{port}.lock")
            try:
                lock.acquire()
            except PlatformError as error:
                if error.code != "ALREADY_OWNED":
                    raise
                continue
            try:
                if not available(port):
                    continue
                yield port
                return
            finally:
                lock.release()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Transfer deadline elapsed waiting for a BBCP port")
        time.sleep(min(0.05, remaining))
