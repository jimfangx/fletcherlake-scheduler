"""Actual private HTTPS/WSS and BBCP infrastructure around one simulated Mac."""

import asyncio
import ssl
import time
from contextlib import ExitStack
from functools import partial
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest
from fl_agent.configuration import confirm_config, write_config
from fl_agent.connection import SchedulerConnection
from fl_agent.service import AgentService
from fl_common.bbcp import BBCP
from fl_gateway.api import create_app
from fl_gateway.store import GatewayStore
from fl_scheduler.db.models import Cluster
from fl_scheduler.registry import Registry
from fl_scheduler.transfers.gateway import GatewayControl
from pydantic import SecretStr
from websockets.asyncio.client import connect

from tests.connected import GatewayServer, until
from tests.dashboard_https import certificate, serve_https
from tests.network_sockets import SocketAdapters
from tests.private_peers import enrolled
from tests.private_ssh import private_ssh

CONTROL = SecretStr("private network acceptance control credential")


def ready_https(peer, url, context):
    peer.wait_connection(urlparse(url).hostname)
    deadline = time.monotonic() + 30
    last_error = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(
                proxy=peer.proxy, verify=context, trust_env=False, timeout=5
            ) as client:
                if client.get(url).status_code in {200, 404}:
                    return
        except httpx.HTTPError as error:
            last_error = error
        time.sleep(0.1)
    raise AssertionError(f"Private HTTPS did not become ready: {last_error}")


@pytest.fixture
async def private_stack(
    scheduler_db, config, headscale_server, tailscale_peers, bbcp_gateway, tmp_path, monkeypatch
):
    coordinator, api_key = headscale_server
    original, public, binary = bbcp_gateway
    store = GatewayStore(original.root, binary, data_port_last=5007)
    public = public.model_copy(update={"data_port_last": 5007})
    peers, addresses = enrolled(
        coordinator,
        api_key,
        tailscale_peers,
        {
            "mac": "tag:cluster",
            "gateway": "tag:transfer-gateway",
            "scheduler": "tag:scheduler",
        },
    )
    adapters = SocketAdapters(tmp_path)
    certificate(tmp_path, ip_addresses=("127.0.0.1", addresses["gateway"], addresses["scheduler"]))
    context = ssl.create_default_context(cafile=str(tmp_path / "server.pem"))
    server = GatewayServer(
        scheduler_db,
        ssl_certfile=str(tmp_path / "server.pem"),
        ssl_keyfile=str(tmp_path / "server.key"),
    )
    await server.start()
    service = None
    try:
        with ExitStack() as stack:
            ssh_port, sources = stack.enter_context(
                private_ssh(tmp_path, adapters, addresses["gateway"], tmp_path / "sshd.conf")
            )
            peers["gateway"].command(
                "serve", "--bg", "--tcp=22", "--proxy-protocol=1", f"tcp://127.0.0.1:{ssh_port}"
            )
            for port in range(5000, 5008):
                peers["gateway"].command(
                    "serve", "--bg", f"--tcp={port}", f"tcp://127.0.0.1:{port}"
                )
            public_origin = stack.enter_context(
                serve_https(lambda origin: create_app(store, CONTROL), tmp_path)
            )
            target = public_origin.removeprefix("https://")
            assert (
                await asyncio.to_thread(
                    httpx.get,
                    public_origin + "/healthz",
                    verify=context,
                    trust_env=False,
                )
            ).status_code == 200
            peers["gateway"].command("serve", "--bg", "--tcp=443", f"tcp://{target}")
            peers["scheduler"].command(
                "serve", "--bg", "--tcp=443", f"tcp://127.0.0.1:{server.port}"
            )
            gateway_origin = f"https://{addresses['gateway']}"
            for peer in (peers["mac"], peers["scheduler"]):
                await asyncio.to_thread(ready_https, peer, gateway_origin + "/healthz", context)
            await asyncio.to_thread(
                ready_https, peers["mac"], f"https://{addresses['scheduler']}/", context
            )
            token = "private-network-agent-token"
            registered = Registry(scheduler_db).register(config, token)
            with scheduler_db.transaction() as session:
                cluster = session.get(Cluster, registered.cluster_id)
                cluster.headscale_addresses = [addresses["mac"]]
            root = tmp_path / "simulated Mac"
            write_config(root, registered)
            confirm_config(root)
            service = AgentService(registered, root, root / "run")
            monkeypatch.setattr(
                "fl_agent.connection.connect",
                partial(
                    connect,
                    proxy=peers["mac"].proxy,
                    ssl=context,
                ),
            )
            service.scheduler_link = SchedulerConnection(
                service,
                f"https://{addresses['scheduler']}",
                token,
                heartbeat_seconds=0.1,
            )
            trace = tmp_path / "private-connect.log"
            transport = BBCP(str(binary), runner=adapters.runner(peers["mac"], trace))
            await service.start()
            service.transfers.transport = transport
            service.exports.transport = transport
            await until(lambda: service.scheduler_link.connected)
            client = httpx.AsyncClient(
                proxy=peers["scheduler"].proxy, verify=context, trust_env=False
            )
            yield SimpleNamespace(
                agent=service,
                store=store,
                public=public,
                private=public.model_copy(update={"host": addresses["gateway"], "port": 22}),
                control=GatewayControl(gateway_origin, CONTROL, client),
                binary=binary,
                trace=trace,
                sources=sources,
                addresses=addresses,
                peers=peers,
                server=server,
            )
    finally:
        if service and hasattr(service, "db"):
            await service.stop()
        await server.stop()
