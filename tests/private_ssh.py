"""A loopback inetd SSH bridge preserves source addresses from tailscaled PROXY metadata."""

import ipaddress
import os
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
from contextlib import contextmanager


@contextmanager
def private_ssh(root, adapters, gateway_ip, ssh_config):
    received = []
    children, sockets = set(), set()
    lock = threading.Lock()
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    with (root / "private-sshd.log").open("w+") as log:

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                child = None
                with lock:
                    sockets.add(self.request)
                try:
                    self.request.settimeout(10)
                    header = bytearray()
                    while not header.endswith(b"\r\n") and len(header) <= 108:
                        byte = self.request.recv(1)
                        if not byte:
                            return
                        header.extend(byte)
                    parts = header.decode("ascii").strip().split()
                    assert len(parts) == 6 and parts[:2] == ["PROXY", "TCP4"]
                    _, _, source, destination, source_port, destination_port = parts
                    # Tailscale's v1 header uses the backend's destination address.
                    # Source identity stays the actual WireGuard peer's address.
                    assert destination in {gateway_ip, "127.0.0.1"}
                    assert 0 < int(destination_port) <= 65535
                    assert ipaddress.ip_address(source) in ipaddress.ip_network("100.64.0.0/10")
                    self.request.settimeout(None)
                    received.append(source)
                    child = subprocess.Popen(
                        [sshd, "-i", "-e", "-f", str(ssh_config)],
                        stdin=self.request,
                        stdout=self.request,
                        stderr=log,
                        env=adapters.server_environment(
                            source, destination, source_port, destination_port
                        ),
                        start_new_session=True,
                    )
                    with lock:
                        children.add(child)
                    child.wait(timeout=60)
                finally:
                    if child:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        child.wait(timeout=5)
                    with lock:
                        children.discard(child)
                        sockets.discard(self.request)

        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
            # Shutdown interrupts accepted sockets and reaps every inetd child.
            server.daemon_threads = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server.server_address[1], received
            finally:
                server.shutdown()
                with lock:
                    for connection in sockets:
                        try:
                            connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        connection.close()
                    for child in children:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                thread.join(timeout=5)
                assert not thread.is_alive()
