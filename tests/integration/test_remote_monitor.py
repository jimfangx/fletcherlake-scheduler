"""Public Python log/state following uses real PostgreSQL and connected agent execution."""

import asyncio

import httpx
from fastapi.testclient import TestClient
from fl_client.api import RemoteClient
from fl_client.credentials import Credentials, CredentialStore
from fl_client.monitor import Monitor
from fl_common.models import JobConfig, ResourceConstraints
from fl_scheduler.agents.commands import Commands
from fl_scheduler.scheduler.placement import Placement

from tests.connected import state, until
from tests.integration.test_artifact_downloads import OWNER


async def test_remote_monitor_follows_binary_uart_and_states_after_a_lost_page(
    scheduler_db, connected_agents, auth_stack, tmp_path
):
    _, services = connected_agents
    worker = next(iter(services[0].workers.values()))
    payload = bytes(range(256)) * 520
    worker.backend.behavior.uart = payload
    worker.backend.behavior.run_seconds = 4
    spec = Placement(scheduler_db).submit(
        JobConfig(resource_constraints=ResourceConstraints(board=worker.board_id), run_timeout=10),
        OWNER,
    )
    Placement(scheduler_db).reserve_pending()
    Commands(scheduler_db).enqueue_after_transfer(spec.job_id)
    await until(lambda: state(scheduler_db, spec.job_id) == "RUNNING")
    sessions, _, _, provider, app = auth_stack
    tokens = await sessions.issue(provider.identity)
    credentials = CredentialStore(tmp_path / "profile" / "credentials.json")
    with credentials.locked():
        credentials.save(Credentials.from_response("https://scheduler.test", tokens.wire()))
    with TestClient(app, base_url="https://scheduler.test") as browser:
        lost, observed_live = False, False

        def handle(request):
            nonlocal lost, observed_live
            response = browser.request(
                request.method,
                request.url.path,
                params=request.url.params,
                headers=request.headers,
                content=request.content,
            )
            if request.url.path.endswith("/logs"):
                body = response.json()
                if body.get("chunk") and body["offset"] == 0:
                    observed_live = state(scheduler_db, spec.job_id) == "RUNNING"
                if not lost and body.get("chunk") and body["offset"] > 0:
                    lost = True
                    raise httpx.ReadError("Injected lost log page after ACK")
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        with RemoteClient(credentials, transport=httpx.MockTransport(handle)) as client:
            monitor = Monitor(client, interval=0.05)
            logs, states = await asyncio.wait_for(
                asyncio.gather(
                    asyncio.to_thread(lambda: b"".join(monitor.logs(spec.job_id, follow=True))),
                    asyncio.to_thread(lambda: list(monitor.states(spec.job_id))),
                ),
                15,
            )
            assert logs == payload and lost and observed_live
            assert states[-1] == "SUCCEEDED" and states.count("RUNNING") == 1
            assert client.status(spec.job_id)["state"] == "SUCCEEDED"
