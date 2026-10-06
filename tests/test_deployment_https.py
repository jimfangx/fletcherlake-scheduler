"""Rendered nginx enforces HTTPS route boundaries, request limits and WebSocket upgrades."""

import httpx
from fl_deploy.manifest import Deployment
from fl_deploy.render import render
from websockets.asyncio.client import connect

from tests.deployment import TEMPLATES, certificates, inventory, require_native_tools
from tests.nginx_runtime import running_nginx, trust_local_certificate, upstream


async def test_real_https_routing_limits_and_private_websocket(tmp_path):
    nginx, _, _ = require_native_tools()
    data = inventory(tmp_path)
    data["certificate_root"] = str(tmp_path / "certificates")
    manifest = Deployment.model_validate(data)
    certificates(tmp_path, manifest)
    files = render(manifest, TEMPLATES)
    root = tmp_path / "nginx"
    root.mkdir(mode=0o700)
    context = trust_local_certificate(tmp_path)
    with (
        upstream() as (port, received),
        running_nginx(nginx, root, files, manifest, port) as origins,
    ):
        async with httpx.AsyncClient(verify=context, trust_env=False, timeout=10) as client:

            async def request(alias, path, host, **kwargs):
                return await client.request(
                    "POST" if "content" in kwargs else "GET",
                    origins[alias] + path,
                    headers={"Host": host, "X-Forwarded-For": "198.51.100.99"},
                    **kwargs,
                )

            scheduler = manifest.scheduler
            gateway = manifest.gateway
            for alias, host, paths in (
                ("127.0.0.1", scheduler.hostname, ["/api/agents/ws"]),
                ("127.0.0.1", scheduler.headscale_hostname, ["/api", "/api/v1/preauthkey"]),
                ("127.0.0.2", scheduler.agent_hostname, ["/", "/api/jobs", "/api/auth/login"]),
                ("127.0.0.3", gateway.hostname, ["/internal/transfers", "/healthz", "/"]),
                ("127.0.0.4", gateway.private_hostname, ["/api/uploads/id/verify", "/"]),
            ):
                before = len(received)
                for path in paths:
                    assert (await request(alias, path, host)).status_code == 404
                assert len(received) == before, "Private/disabled route reached the upstream"
            # Choosing a private Host header on the public interface cannot open it.
            assert (
                await request("127.0.0.1", "/api/agents/ws", scheduler.agent_hostname)
            ).status_code == 404
            result = await request("127.0.0.1", "/api/jobs", scheduler.hostname)
            assert result.status_code == 200
            assert result.json()["forwarded_for"] == "127.0.0.1"
            assert result.json()["forwarded_proto"] == "https"
            assert (
                await request("127.0.0.1", "/ts2021", scheduler.headscale_hostname)
            ).status_code == 200
            assert (
                await request("127.0.0.4", "/internal/transfers", gateway.private_hostname)
            ).status_code == 200
            large = b"a" * (3 * 1024 * 1024)
            assert (
                await request("127.0.0.1", "/api/jobs", scheduler.hostname, content=large)
            ).status_code == 413
            unregister = "/api/enrollment/clusters/00000000-0000-0000-0000-000000000001/unregister"
            result = await request("127.0.0.1", unregister, scheduler.hostname, content=large)
            assert result.status_code == 200 and result.json()["size"] == len(large)
            assert (
                await request(
                    "127.0.0.2", "/api/agents/snapshot", scheduler.agent_hostname, content=large
                )
            ).status_code == 200
            assert (
                await request(
                    "127.0.0.3", "/api/uploads/id/verify", gateway.hostname, content=b"a" * 17000
                )
            ).status_code == 413
            for alias, host, path, count in (
                ("127.0.0.1", scheduler.hostname, "/api/auth/login?code=PRIVATE_OAUTH_MARKER", 25),
                ("127.0.0.1", scheduler.hostname, "/api/enrollment/ticket", 10),
                ("127.0.0.3", gateway.hostname, "/api/uploads/id/verify", 10),
            ):
                statuses = [(await request(alias, path, host)).status_code for _ in range(count)]
                assert 200 in statuses and 429 in statuses, statuses
            uri = origins["127.0.0.2"].replace("https:", "wss:") + "/api/agents/ws"
            async with connect(
                uri, ssl=context, open_timeout=5, close_timeout=5, proxy=None
            ) as websocket:
                await websocket.send("private agent snapshot")
                assert await websocket.recv() == "private agent snapshot"
        assert "PRIVATE_OAUTH_MARKER" not in (root / "nginx.log").read_text()
