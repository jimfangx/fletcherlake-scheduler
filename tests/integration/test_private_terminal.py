"""A non-member terminal submits inputs and retrieves results through real private peers."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from fl_client.api import RemoteClient
from fl_client.credentials import Credentials, CredentialStore
from fl_client.receipts import ReceiptStore
from fl_client.results import Results
from fl_client.submission import Submission
from fl_common.bbcp import BBCP
from fl_common.models import JobConfig
from fl_gateway.api import create_app as gateway_app
from fl_scheduler.artifacts.api import Downloads
from fl_scheduler.artifacts.service import ExportService
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.service import create_app
from fl_scheduler.transfers.service import TransferService
from fl_scheduler.transfers.submissions import Submissions

from tests.connected import state, until
from tests.private_stack import CONTROL


@pytest.mark.parametrize(
    "tailscale_peers", [False, True], indirect=True, ids=["normal", "derp-only"]
)
async def test_terminal_submission_execution_and_results_over_private_peers(
    scheduler_db, private_stack, auth_stack, tmp_path
):
    network = private_stack
    sessions, login, _, provider, _ = auth_stack
    tokens = await sessions.issue(provider.identity)
    credentials = CredentialStore(tmp_path / "client" / "credentials.json")
    with credentials.locked():
        credentials.save(Credentials.from_response("https://scheduler.test", tokens.wire()))
    transfers = TransferService(scheduler_db, network.control, network.private)
    submissions = Submissions(scheduler_db, transfers, network.public, "https://gateway.test")
    downloads = Downloads(
        ExportService(scheduler_db, network.control, network.private),
        network.public,
    )
    scheduler = create_app(
        scheduler_db,
        AuthAPI(sessions, login, "https://scheduler.test"),
        transfers=transfers,
        submissions=submissions,
        downloads=downloads,
    )
    original_lifespan = scheduler.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        # The private HTTP pool belongs to the scheduler's actual service loop.
        async with network.control.client, original_lifespan(app):
            yield

    scheduler.router.lifespan_context = lifespan
    source, bitstream = tmp_path / "input.elf", tmp_path / "FPGA.bit"
    source.write_bytes(b"private terminal ELF\n" * 4096)
    bitstream.write_bytes(b"private terminal bitstream\n" * 4096)
    receipt = ReceiptStore(tmp_path / "receipt" / "submission.json")
    with (
        TestClient(scheduler, base_url="https://scheduler.test") as browser,
        TestClient(gateway_app(network.store, CONTROL), base_url="https://gateway.test") as gateway,
    ):

        def handle(request):
            assert request.url.host in {"scheduler.test", "gateway.test"}
            backend = browser if request.url.host == "scheduler.test" else gateway
            response = backend.request(
                request.method,
                request.url.path,
                headers=request.headers,
                content=request.content,
            )
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        with RemoteClient(credentials, transport=httpx.MockTransport(handle)) as client:
            # Only public endpoints are visible to this terminal; no peer/proxy is
            # attached to its native BBCP transport or human HTTP client.
            transport = BBCP(str(network.binary))
            workflow = Submission(client, bbcp=transport, display=lambda line: None)
            spec = await asyncio.wait_for(
                asyncio.to_thread(
                    workflow.run,
                    receipt,
                    JobConfig(binary=str(source), bitstream=str(bitstream), run_timeout=3),
                ),
                45,
            )
            await until(lambda: state(scheduler_db, spec.job_id) == "SUCCEEDED", seconds=30)
            destination = tmp_path / "returned results"
            results = Results(client, bbcp=transport, display=lambda line: None)
            paths = await asyncio.wait_for(
                asyncio.to_thread(
                    results.run,
                    spec.job_id,
                    destination,
                    inputs=True,
                ),
                90,
            )
            assert paths
            assert (destination / "binary").read_bytes() == source.read_bytes()
            assert (destination / "bitstream").read_bytes() == bitstream.read_bytes()
            assert (destination / "results.json").read_bytes() == network.agent.store.path(
                spec.job_id, "results"
            ).read_bytes()
            assert await asyncio.to_thread(workflow.run, receipt) == spec
        assert network.agent.scheduler_link.connected
        assert network.addresses["mac"] in network.sources
        connections = [line.split() for line in network.trace.read_text().splitlines()]
        assert len([port for _, port in connections if port == "22"]) >= 7
        assert all(host == network.addresses["gateway"] for host, _ in connections)
        network.peers["mac"].assert_relay(network.addresses["gateway"])
        network.peers["mac"].assert_relay(network.addresses["scheduler"])
        network.peers["scheduler"].assert_relay(network.addresses["gateway"])
        assert (
            sum(event.type == "JOB_RUNNING" for event in network.agent.db.job_events(spec.job_id))
            == 1
        )
