"""Actual encrypted peer traffic and ACL isolation with the shipped Headscale policy."""

import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fl_common.models.base import utcnow
from fl_scheduler.network.headscale import Headscale
from pydantic import SecretStr

from tests.tailscale import private_get, scoped_key


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"private scheduler reply"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def wait_reply(peer, url):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            response = private_get(peer, url)
            if response.status_code == 200:
                return response
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    pytest.fail("Enrolled private peers did not establish the permitted connection")


@pytest.mark.parametrize(
    "tailscale_peers", [False, True], indirect=True, ids=["normal", "derp-only"]
)
async def test_private_peers_enforce_cluster_scheduler_acl(headscale_server, tailscale_peers):
    coordinator, api_key = headscale_server
    cluster, scheduler, other_cluster = [tailscale_peers() for _ in range(3)]
    async with httpx.AsyncClient() as client:
        provider = Headscale(coordinator, "https://headscale.test", SecretStr(api_key), client)
        key = await provider.create_cluster_key(utcnow() + timedelta(minutes=10))
        cluster_ip = cluster.join(coordinator, key.key.get_secret_value(), "cluster")
        joined = await provider.joined_node(key.key_id, cluster.node_key)
        assert cluster_ip in joined.addresses
    scheduler_ip = scheduler.join(
        coordinator, scoped_key(coordinator, api_key, "tag:scheduler"), "scheduler"
    )
    other_ip = other_cluster.join(
        coordinator, scoped_key(coordinator, api_key, "tag:cluster"), "other-cluster"
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        target = f"tcp://127.0.0.1:{server.server_port}"
        try:
            for peer in (scheduler, other_cluster):
                peer.command("serve", "--bg", "--tcp=443", target)
            scheduler.command("serve", "--bg", "--tcp=8443", target)
            assert wait_reply(cluster, f"http://{scheduler_ip}:443").content == (
                b"private scheduler reply"
            )
            # Prove the second listener is real using another permitted flow first.
            assert wait_reply(scheduler, f"http://{other_ip}:443").status_code == 200
            for blocked in (f"http://{other_ip}:443", f"http://{scheduler_ip}:8443"):
                with pytest.raises(httpx.HTTPError):
                    response = private_get(cluster, blocked)
                    response.raise_for_status()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize("tailscale_peers", [True], indirect=True, ids=["derp-only"])
async def test_new_cluster_can_reach_existing_gateway(headscale_server, tailscale_peers):
    coordinator, api_key = headscale_server
    cluster, gateway = tailscale_peers(), tailscale_peers()
    cluster.join(coordinator, scoped_key(coordinator, api_key, "tag:cluster"), "first-cluster")
    address = gateway.join(
        coordinator, scoped_key(coordinator, api_key, "tag:transfer-gateway"), "gateway"
    )
    gateway_pid = gateway.process.pid
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            gateway.command("serve", "--bg", "--tcp=443", f"tcp://127.0.0.1:{server.server_port}")
            assert wait_reply(cluster, f"http://{address}:443").status_code == 200
            # Joining another tagged node must update an already connected receiver's ACL.
            for name in ("second-cluster", "third-cluster"):
                added = tailscale_peers()
                added.join(coordinator, scoped_key(coordinator, api_key, "tag:cluster"), name)
                assert wait_reply(added, f"http://{address}:443").status_code == 200
                added.assert_relay(address)
            assert gateway.process.pid == gateway_pid and gateway.process.poll() is None
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
