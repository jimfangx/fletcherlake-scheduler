"""Disposable real Headscale lifecycle, including restart with persistent node state."""

import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from tests.headscale_config import write_config


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HeadscaleProcess:
    def __init__(self, binary, root):
        self.binary = binary
        port = free_port()
        self.path = write_config(root, port, free_port(), free_port())
        self.url = f"http://127.0.0.1:{port}"
        self.log = (root / "server.log").open("a+")
        self.process = None
        self.api_key = None

    def start(self):
        assert self.process is None or self.process.poll() is not None
        self.process = subprocess.Popen(
            [self.binary, "--config", str(self.path), "serve"],
            stdout=self.log,
            stderr=self.log,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert self.process.poll() is None, "Headscale exited before becoming ready"
            try:
                if httpx.get(self.url + "/health", timeout=0.5, trust_env=False).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError("Headscale did not become ready")

    def stop(self):
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def create_api_key(self):
        result = subprocess.run(
            [
                self.binary,
                "--config",
                str(self.path),
                "apikeys",
                "create",
                "--expiration",
                "1h",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, "Headscale API key creation failed"
        self.api_key = json.loads(result.stdout)
        assert isinstance(self.api_key, str)


@pytest.fixture
def headscale_process():
    binary = os.environ.get("FL_TEST_HEADSCALE")
    if not binary:
        pytest.skip("Set FL_TEST_HEADSCALE to the checksum-verified Headscale 0.29.4 binary")
    with tempfile.TemporaryDirectory(prefix="fl-hs-") as name:
        coordinator = HeadscaleProcess(binary, Path(name))
        try:
            coordinator.start()
            coordinator.create_api_key()
            yield coordinator
        finally:
            coordinator.stop()
            coordinator.log.close()


@pytest.fixture
def headscale_server(headscale_process):
    return headscale_process.url, headscale_process.api_key
