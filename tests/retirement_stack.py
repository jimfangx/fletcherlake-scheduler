"""A revoked node can retry retirement; re-enrollment starts a fresh daemon and history."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from fl import Cluster, ClusterSetup
from fl.macos.networking import TailscaleClient
from fl_agent.api import create_app as agent_app
from fl_agent.configuration import load_config
from fl_agent.hardware.mock import MockBehavior, MockBoardBackend
from fl_agent.service import AgentService
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.auth.google import Identity
from fl_scheduler.network.cleanup import NetworkCleanup
from fl_scheduler.network.enrollment import EnrollmentService
from fl_scheduler.service import create_app
from pydantic import SecretStr

from tests.native_macos import Launchd
from tests.network_helpers import MockNetwork


@pytest.fixture
async def retirement_stack(auth_stack, scheduler_db, tmp_path):
    sessions, login, directory, _, _ = auth_stack
    directory.members["admins"].add("admin@example.edu")
    admin = sessions.issue_authorized(Identity("admin-subject", "admin@example.edu"))
    network = MockNetwork()
    enrollment = EnrollmentService(
        scheduler_db,
        network,
        "https://headscale.test",
        SecretStr(Fernet.generate_key().decode()),
        agent_origin="https://agent.scheduler.test",
    )
    app = create_app(
        scheduler_db,
        AuthAPI(sessions, login, "https://scheduler.test"),
        enrollment=enrollment,
        maintenance=False,
    )
    controls = {"lose_unregister": True}
    loop = asyncio.get_running_loop()
    services, calls, joins, unregister_requests = [], [], [], []
    state, runtime = tmp_path / "state", tmp_path / "run"
    (tmp_path / "pixi.toml").write_text("placeholder native project")
    native = Launchd(calls, agent_registered=False)

    async def start():
        service = AgentService(
            load_config(state),
            state,
            runtime,
            {"board-0": MockBoardBackend(MockBehavior(hang=True))},
        )
        await service.start()
        services.append(service)

    native.on_start = lambda: asyncio.run_coroutine_threadsafe(start(), loop).result(15)
    native.on_stop = lambda: asyncio.run_coroutine_threadsafe(services[-1].stop(), loop).result(15)

    async def local_request(request):
        async with httpx.AsyncClient(
            base_url="http://fl-agent", transport=httpx.ASGITransport(app=agent_app(services[-1]))
        ) as client:
            return await client.request(
                request.method, request.url.path, content=request.content, headers=request.headers
            )

    def local(socket):
        assert socket == runtime / "agent.sock"

        def bridge_local(request):
            response = asyncio.run_coroutine_threadsafe(local_request(request), loop).result(15)
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        transport = httpx.MockTransport(bridge_local)
        return Cluster(httpx.Client(base_url="http://fl-agent", transport=transport))

    def tailscale(argv):
        assert "private-join-key" not in repr(argv)
        if argv[1] == "up":
            path = Path(argv[argv.index("--auth-key") + 1].removeprefix("file:"))
            joins.append(network.join(path.read_text().removeprefix("private-join-key-")))
            return ""
        if argv[1] == "logout":
            calls.append("logout")
            return ""
        return json.dumps({"BackendState": "Running", "Self": {"PublicKey": joins[-1].node_key}})

    try:
        with TestClient(app, base_url="https://scheduler.test") as server:

            def ticket():
                response = server.post(
                    "/api/admin/enrollments",
                    json={},
                    headers={
                        "Authorization": "Bearer " + admin.access_token.get_secret_value(),
                    },
                )
                assert response.status_code == 201
                return response.json()

            def bridge(request):
                assert request.url.host == "scheduler.test" and request.url.scheme == "https"
                response = server.request(
                    request.method,
                    request.url.path,
                    headers=request.headers,
                    content=request.content,
                )
                if request.url.path.endswith("/unregister") and response.status_code == 202:
                    unregister_requests.append(request.url.path)
                    if len(unregister_requests) == 1 and controls["lose_unregister"]:
                        # Retirement commits and node revocation run before its response is lost.
                        asyncio.run_coroutine_threadsafe(
                            NetworkCleanup(enrollment).tick(), loop
                        ).result(15)
                        raise httpx.ReadError("Lost unregister response", request=request)
                return httpx.Response(
                    response.status_code, headers=response.headers, content=response.content
                )

            setup = ClusterSetup(
                state,
                runtime,
                authorize=lambda: None,
                launchd_root=tmp_path / "launchd",
                pixi="/opt/pixi/bin/pixi",
                runner=native.run,
                probe=native.loaded,
                client_factory=local,
                http_factory=lambda: httpx.Client(transport=httpx.MockTransport(bridge)),
                network_factory=lambda: TailscaleClient(runner=tailscale, executable="tailscale"),
            )
            yield SimpleNamespace(
                setup=setup,
                ticket=ticket,
                services=services,
                calls=calls,
                network=network,
                native=native,
                admin=admin,
                server=server,
                joins=joins,
                unregister_requests=unregister_requests,
                controls=controls,
            )
    finally:
        for service in services:
            if service.lock.held:
                await service.stop()
