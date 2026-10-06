"""The real relay forwards two fixed license ports on its private interface only."""

import socket
import socketserver
import subprocess
import threading
import time
from contextlib import contextmanager

import pytest
from fl_deploy.manifest import Deployment
from fl_deploy.render import render

from tests.deployment import TEMPLATES, inventory, require_native_tools, run


@contextmanager
def license_backend(label):
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(3)
            data = self.request.recv(1024)
            if data:
                self.request.sendall(label + data)

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo) as server:
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_real_private_fixed_port_relay(tmp_path):
    _, haproxy, _ = require_native_tools()
    with license_backend(b"manager:") as manager, license_backend(b"vendor:") as vendor:
        data = inventory(tmp_path, license_relay=True)
        data["license_relay"].update(manager_port=manager, vendor_port=vendor)
        files = render(Deployment.model_validate(data), TEMPLATES)
        source = files["license-relay/haproxy.cfg"]
        # Runtime-only address mapping leaves the rendered fixed port pair intact.
        source = (
            source.replace("bind 100.64.0.30:", "bind 127.0.0.2:")
            .replace("server manager 192.0.2.30:", "server manager 127.0.0.1:")
            .replace("server vendor 192.0.2.30:", "server vendor 127.0.0.1:")
        )
        config = tmp_path / "haproxy.cfg"
        config.write_text(source)
        run(haproxy, "-c", "-f", str(config))
        with (tmp_path / "haproxy.log").open("w+") as log:
            process = subprocess.Popen([haproxy, "-db", "-f", str(config)], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    assert process.poll() is None, "HAProxy exited before readiness"
                    try:
                        with socket.create_connection(("127.0.0.2", manager), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.01)
                else:
                    raise AssertionError("HAProxy did not become ready")
                for port, label in ((manager, b"manager:"), (vendor, b"vendor:")):
                    with socket.create_connection(("127.0.0.2", port), timeout=3) as connection:
                        connection.sendall(b"license checkout/release fixture")
                        assert connection.recv(1024) == label + b"license checkout/release fixture"
                    with pytest.raises(ConnectionRefusedError):
                        socket.create_connection(("127.0.0.3", port), timeout=1)
                # Reserve a non-listening port; ordinary host services may own 22.
                with socket.socket() as unused:
                    unused.bind(("127.0.0.2", 0))
                    with pytest.raises(ConnectionRefusedError):
                        socket.create_connection(unused.getsockname(), timeout=1)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
