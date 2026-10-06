"""Isolated userspace peers for the real Headscale network acceptance test."""

import json
import os
import subprocess
import tempfile
import time
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fl_common.files import atomic_write
from fl_common.models.base import utcnow

from tests.headscale import free_port
from tests.peer_diagnostics import retain_transport_state


class Peer:
    def __init__(self, binaries: Path, root: Path, stack: ExitStack, *, force_derp=False):
        self.binary = binaries / "tailscale"
        self.force_derp = force_derp
        self.socket = root / "tailscaled.sock"
        self.proxy = f"http://127.0.0.1:{free_port()}"
        self.logs_path = root / "peer.log"
        logs = stack.enter_context(self.logs_path.open("w"))
        self.process = subprocess.Popen(
            [
                str(binaries / "tailscaled"),
                "--tun=userspace-networking",
                "--no-logs-no-support",
                "--port=0",
                f"--state={root / 'state.json'}",
                f"--socket={self.socket}",
                f"--outbound-http-proxy-listen={self.proxy.removeprefix('http://')}",
            ],
            stdout=logs,
            stderr=logs,
            env={
                **os.environ,
                "TS_NO_LOGS_NO_SUPPORT": "true",
                "TS_DEBUG_USE_DERP_HTTP": "true",
                "TS_DEBUG_ALWAYS_USE_DERP": str(force_derp).lower(),
            },
        )
        stack.callback(self.stop)
        deadline = time.monotonic() + 10
        while not self.socket.exists():
            assert self.process.poll() is None, "Tailscaled exited before opening its socket"
            assert time.monotonic() < deadline, "Tailscaled did not open its socket"
            time.sleep(0.05)

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def command(self, *args):
        result = subprocess.run(
            [str(self.binary), f"--socket={self.socket}", *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # Never include daemon output or auth material in an assertion failure.
        assert result.returncode == 0, f"Tailscale {args[0]} failed ({result.returncode})"
        return result.stdout

    def join(self, coordinator: str, key: str, name: str):
        path = self.socket.parent / "join.key"
        path.touch(mode=0o600)
        path.write_text(key)
        try:
            self.command(
                "up",
                f"--login-server={coordinator}",
                f"--auth-key=file:{path}",
                f"--hostname={name}",
                "--accept-dns=false",
                "--accept-routes=false",
                "--timeout=20s",
            )
        finally:
            path.unlink()
        status = json.loads(self.command("status", "--json"))
        assert status["BackendState"] == "Running"
        self.node_key = status["Self"]["PublicKey"]
        self.name = name
        self.wait_relay()
        return next(ip for ip in status["Self"]["TailscaleIPs"] if ":" not in ip)

    def wait_relay(self, address=None):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = json.loads(self.command("status", "--json"))
            targets = (
                [status["Self"]]
                if address is None
                else [
                    peer
                    for peer in status.get("Peer", {}).values()
                    if address in peer["TailscaleIPs"]
                ]
            )
            if targets and all(peer.get("Relay") == "test" for peer in targets):
                return
            time.sleep(0.1)
        raise AssertionError(f"Local relay route not advertised for {address or 'self'}")

    def assert_relay(self, address):
        if not self.force_derp:
            return
        status = json.loads(self.command("status", "--json"))
        target = next(peer for peer in status["Peer"].values() if address in peer["TailscaleIPs"])
        assert target["Relay"] == "test" and not target["CurAddr"]
        assert target["RxBytes"] > 0 and target["TxBytes"] > 0

    def wait_connection(self, address):
        self.wait_relay(address)
        # TSMP tests the encrypted peer path before a host TCP listener is involved.
        self.command("ping", "--tsmp", "--until-direct=false", "--c=3", "--timeout=5s", address)


@pytest.fixture
def tailscale_peers(request, tmp_path):
    directory = os.environ.get("FL_TEST_TAILSCALE_DIR")
    if not directory:
        pytest.skip("Set FL_TEST_TAILSCALE_DIR to checksum-verified Tailscale binaries")
    binaries = Path(directory)
    assert (binaries / "tailscale").is_file() and (binaries / "tailscaled").is_file()
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="fl-ts-")))
        peers = []

        def make():
            path = root / str(len(list(root.iterdir())))
            path.mkdir(mode=0o700)
            peer = Peer(binaries, path, stack, force_derp=getattr(request, "param", False))
            peers.append(peer)
            return peer

        try:
            yield make
        finally:
            # Retain only transport diagnostics, excluding control/auth/join output.
            markers = ("magicsock:", "netstack:", "derphttp.", "wg:")
            for index, peer in enumerate(peers):
                retain_transport_state(peer, tmp_path / f"peer-{index}-transport-state.json")
                lines = peer.logs_path.read_text().splitlines()
                selected = "\n".join(line for line in lines if any(m in line for m in markers))
                atomic_write(tmp_path / f"peer-{index}-transport.log", selected.encode())


def private_get(peer: Peer, url: str, timeout: float = 2):
    # The proxy routes requests through userspace WireGuard, not the host route table.
    with httpx.Client(proxy=peer.proxy, trust_env=False, timeout=timeout) as client:
        return client.get(url)


def scoped_key(url, api_key, tag):
    response = httpx.post(
        url + "/api/v1/preauthkey",
        headers={"Authorization": "Bearer " + api_key},
        json={
            "reusable": False,
            "ephemeral": False,
            "expiration": (utcnow() + timedelta(minutes=10)).isoformat(),
            "aclTags": [tag],
        },
    )
    response.raise_for_status()
    return response.json()["preAuthKey"]["key"]
