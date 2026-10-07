"""Public terminal submission through real SSH/Rclone and connected mock Mac agents."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from fl_client.api import RemoteClient
from fl_client.credentials import Credentials, CredentialStore
from fl_client.receipts import ReceiptStore
from fl_client.results import Results
from fl_client.submission import Submission
from fl_common.models import JobConfig, ResourceConstraints
from fl_common.rclone import Rclone
from fl_gateway.api import create_app as gateway_app
from fl_scheduler.artifacts.api import Downloads
from fl_scheduler.artifacts.service import ExportService
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.db.models import Command, Event, Job
from fl_scheduler.service import create_app
from fl_scheduler.transfers.gateway import GatewayControl
from fl_scheduler.transfers.models import Upload
from fl_scheduler.transfers.service import TransferService
from fl_scheduler.transfers.submissions import Submissions
from pydantic import SecretStr
from sqlalchemy import func, select

from tests.connected import state, until

CONTROL = SecretStr("terminal acceptance private control credential")


class CountingRclone(Rclone):
    def __init__(self, binary):
        super().__init__(str(binary))
        self.copies = []

    async def copy(self, source, endpoint, grant, kind, identity):
        self.copies.append((grant.transfer_id, kind))
        await super().copy(source, endpoint, grant, kind, identity)


@pytest.mark.parametrize("lost_path", ["/api/submissions", "/delivery"])
async def test_public_terminal_submission_recovers_lost_responses_without_duplicate_execution(
    scheduler_db, connected_agents, rclone_gateway, auth_stack, tmp_path, lost_path
):
    store, endpoint, binary = rclone_gateway
    sessions, login, _, provider, _ = auth_stack
    tokens = await sessions.issue(provider.identity)
    credentials = CredentialStore(tmp_path / "client" / "credentials.json")
    with credentials.locked():
        credentials.save(Credentials.from_response("https://scheduler.test", tokens.wire()))
    receipt_store = ReceiptStore(tmp_path / "submission" / "receipt.json")
    display, calls = [], []
    target = connected_agents[1][0]
    target.transfers.transport = Rclone(str(binary))
    target.exports.transport = Rclone(str(binary))
    source = tmp_path / "input with spaces.elf"
    source.write_bytes(b"ordinary user payload\n" * 8192)
    bitstream = tmp_path / "FPGA with spaces.bit"
    bitstream.write_bytes(b"ordinary user bitstream\n" * 4096)
    config = JobConfig(
        binary=str(source),
        bitstream=str(bitstream),
        run_timeout=3,
        resource_constraints=ResourceConstraints(board=next(iter(target.workers))),
    )
    api = gateway_app(store, CONTROL)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api)) as internal:
        transfers = TransferService(
            scheduler_db,
            GatewayControl("https://gateway.test", CONTROL, internal),
            endpoint,
            simulation_networks=("127.0.0.1/32",),
        )
        submissions = Submissions(scheduler_db, transfers, endpoint, "https://gateway.test")
        downloads = Downloads(
            ExportService(
                scheduler_db,
                transfers.gateway,
                endpoint,
                simulation_networks=("127.0.0.1/32",),
            ),
            endpoint,
        )
        scheduler = create_app(
            scheduler_db,
            AuthAPI(sessions, login, "https://scheduler.test"),
            transfers=transfers,
            submissions=submissions,
            downloads=downloads,
        )
        with (
            TestClient(scheduler, base_url="https://scheduler.test") as browser,
            TestClient(api, base_url="https://gateway.test") as gateway,
        ):
            lost = False

            def handle(request):
                nonlocal lost
                calls.append(request)
                backend = browser if request.url.host == "scheduler.test" else gateway
                assert request.url.host in {"scheduler.test", "gateway.test"}
                response = backend.request(
                    request.method,
                    request.url.path,
                    headers=request.headers,
                    content=request.content,
                )
                if not lost and request.url.path.endswith(lost_path):
                    assert response.is_success
                    lost = True
                    raise httpx.ReadError("Injected lost response after server commit")
                return httpx.Response(
                    response.status_code, headers=response.headers, content=response.content
                )

            transport = CountingRclone(binary)
            with RemoteClient(credentials, transport=httpx.MockTransport(handle)) as client:
                workflow = Submission(client, rclone=transport, display=display.append)
                with pytest.raises(httpx.ReadError, match="lost response"):
                    await asyncio.wait_for(
                        asyncio.to_thread(workflow.run, receipt_store, config), 30
                    )
                with receipt_store.lock():
                    pending = receipt_store.load()
                    assert not pending.delivered
                    assert (pending.spec is None) == (lost_path == "/api/submissions")
                spec = await asyncio.wait_for(asyncio.to_thread(workflow.run, receipt_store), 30)
                await until(lambda: state(scheduler_db, spec.job_id) == "SUCCEEDED", seconds=25)
                again = await asyncio.to_thread(workflow.run, receipt_store)
                assert again == spec
                assert [kind for _, kind in transport.copies] == ["binary", "bitstream"]
                results = Results(client, rclone=transport, display=display.append)
                destination = tmp_path / "downloaded results"
                paths = await asyncio.wait_for(
                    asyncio.to_thread(results.run, spec.job_id, destination, inputs=True),
                    90,  # Five files traverse both Rclone hops, each with a fresh guardian process.
                )
                copies = list(transport.copies)
                assert (destination / "binary").read_bytes() == source.read_bytes()
                assert (destination / "bitstream").read_bytes() == bitstream.read_bytes()
                assert (destination / "results.json").read_bytes() == target.store.path(
                    spec.job_id, "results"
                ).read_bytes()
                assert await asyncio.to_thread(results.run, spec.job_id, destination) == paths
                assert transport.copies == copies
            with receipt_store.lock():
                receipt = receipt_store.load()
            assert receipt.delivered and receipt.spec == spec
            assert receipt_store.path.stat().st_mode & 0o777 == 0o600
            assert receipt_store.identity.stat().st_mode & 0o777 == 0o600
            assert receipt_store.path.parent.stat().st_mode & 0o777 == 0o700
            staging = receipt.staging_token.get_secret_value()
            access = tokens.access_token.get_secret_value()
            for call in calls:
                if call.url.host == "gateway.test":
                    assert call.headers["authorization"] == "Bearer " + staging
                    assert access not in call.content.decode()
                else:
                    assert staging not in call.content.decode()
            assert all(staging not in line and access not in line for line in display)
            with scheduler_db.transaction() as session:
                assert session.scalar(select(func.count()).select_from(Job)) == 1
                assert session.scalar(select(func.count()).select_from(Upload)) == 1
                job = session.get(Job, spec.job_id)
                assert job.owner == provider.identity.email
                assert staging not in str(job.spec) and "public_key" not in str(job.spec)
                created = list(
                    session.scalars(
                        select(Event).where(Event.type == "JOB_CREATED", Event.cluster_id.is_(None))
                    )
                )
                assert len(created) == 1
                commands = list(session.scalars(select(Command)))
                assert all(staging not in str(command.envelope) for command in commands)
            assert (
                sum(event.type == "JOB_RUNNING" for event in target.db.job_events(spec.job_id)) == 1
            )
