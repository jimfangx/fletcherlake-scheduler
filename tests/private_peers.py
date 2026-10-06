"""Named real peers and bounded readiness for native userspace transfer acceptance."""

import socket
import time
from urllib.parse import urlparse

from tests.tailscale import scoped_key


def enrolled(coordinator, api_key, make, roles):
    peers, addresses = {}, {}
    for name, tag in roles.items():
        peer = make()
        addresses[name] = peer.join(coordinator, scoped_key(coordinator, api_key, tag), name)
        peers[name] = peer
    return peers, addresses


def ready_ssh(address, clients):
    for peer in clients:
        peer.wait_connection(address)
        deadline = time.monotonic() + 30
        last_error = None
        while time.monotonic() < deadline:
            proxy = urlparse(peer.proxy)
            phase = "CONNECT"
            try:
                with socket.create_connection(
                    (proxy.hostname, proxy.port), timeout=5
                ) as connection:
                    request = f"CONNECT {address}:22 HTTP/1.1\r\nHost: {address}:22\r\n\r\n"
                    connection.sendall(request.encode("ascii"))
                    header = read_until(connection, b"\r\n\r\n", 4096)
                    assert header.startswith(b"HTTP/1.1 200 "), "Private proxy rejected CONNECT"
                    phase = "SSH banner"
                    if read_until(connection, b"\r\n", 256).startswith(b"SSH-2.0-"):
                        break
            except OSError as error:
                last_error = f"{phase}: {error}"
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"Private SSH from {peer.name} to {address} did not become ready: {last_error}"
            )


def read_until(connection, terminator, limit):
    # Buffered CONNECT readers can consume and discard a pending SSH banner.
    data = bytearray()
    while not data.endswith(terminator) and len(data) < limit:
        byte = connection.recv(1)
        if not byte:
            raise OSError("Private probe connection closed")
        data.extend(byte)
    assert data.endswith(terminator), "Private probe frame exceeded its limit"
    return bytes(data)
