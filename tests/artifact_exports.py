"""Real gateway control and a terminal job for export retry/deletion acceptance."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fl_common.models.download import DownloadRequest
from fl_common.rclone import Rclone
from fl_common.ssh import create_identity
from fl_gateway.api import create_app
from fl_scheduler.artifacts.api import Downloads
from fl_scheduler.artifacts.models import Download
from fl_scheduler.artifacts.service import ExportService
from fl_scheduler.transfers.gateway import GatewayControl
from pydantic import SecretStr

from tests.connected import until
from tests.integration.test_artifact_downloads import OWNER, completed


@pytest.fixture
async def export_stack(scheduler_db, connected_agents, rclone_gateway, tmp_path):
    store, endpoint, binary = rclone_gateway
    spec = await completed(scheduler_db)
    target = next(
        agent
        for agent in connected_agents[1]
        if any(record.spec.job_id == spec.job_id for record in agent.db.jobs())
    )
    target.exports.transport = Rclone(str(binary))
    control = SecretStr("artifact export private gateway credential")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, control))
    ) as client:
        gateway = GatewayControl("https://gateway.test", control, client)
        service = ExportService(
            scheduler_db, gateway, endpoint, simulation_networks=("127.0.0.1/32",)
        )
        downloads = Downloads(service, endpoint)
        identity = tmp_path / "result-reader"
        body = DownloadRequest(
            request_id=uuid4(), public_key=create_identity(identity), kinds=["results"]
        )
        ticket = await downloads.request(spec.job_id, body, OWNER)
        assert ticket.state == "WAITING"
        with scheduler_db.transaction() as session:
            export_id = session.get(Download, ticket.download_id).export_id
        await until(lambda: service.state.work(export_id).prepare is not None)
        yield SimpleNamespace(
            db=scheduler_db,
            store=store,
            endpoint=endpoint,
            binary=binary,
            spec=spec,
            target=target,
            service=service,
            downloads=downloads,
            body=body,
            export_id=export_id,
            identity=identity,
        )
