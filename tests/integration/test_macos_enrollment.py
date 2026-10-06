"""Mac setup through public HTTP endpoints; only macOS command execution is injected."""

import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from fl import ClusterSetup
from fl_agent.configuration import config_confirmed, load_config
from fl_agent.credentials import load_credentials
from fl_agent.locks import ExclusiveLock
from fl_cli.enrollment import MacEnrollment
from fl_cli.networking import TailscaleClient
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.auth.google import Identity
from fl_scheduler.db.models import Cluster
from fl_scheduler.network.enrollment import EnrollmentService
from fl_scheduler.service import create_app
from pydantic import SecretStr

from tests.network_helpers import MockNetwork


def test_macos_setup_recovers_lost_registration_response(
    auth_stack, scheduler_db, config, tmp_path
):
    sessions, login, directory, _, _ = auth_stack
    directory.members["admins"].add("admin@example.edu")
    admin_tokens = sessions.issue_authorized(Identity("admin-subject", "admin@example.edu"))
    network = MockNetwork()
    service = EnrollmentService(
        scheduler_db,
        network,
        "https://headscale.test",
        SecretStr(Fernet.generate_key().decode()),
        agent_origin="https://agent.scheduler.test",
    )
    app = create_app(
        scheduler_db,
        AuthAPI(sessions, login, "https://scheduler.test"),
        enrollment=service,
        maintenance=False,
    )
    root = tmp_path / "mac-state"
    joined = []
    calls = []

    def tailscale(argv):
        calls.append(argv)
        assert "private-join-key" not in repr(argv)
        if argv[1] == "up":
            key_file = Path(argv[argv.index("--auth-key") + 1].removeprefix("file:"))
            assert key_file.stat().st_mode & 0o777 == 0o600
            assert key_file.read_text() == "private-join-key-1"
            joined.append(network.join("1"))
            return ""
        assert argv[1:] == ["status", "--json"]
        return json.dumps({"BackendState": "Running", "Self": {"PublicKey": joined[0].node_key}})

    with TestClient(app, base_url="https://scheduler.test") as server:
        response = server.post(
            "/api/admin/enrollments",
            json={},
            headers={
                "Authorization": "Bearer " + admin_tokens.access_token.get_secret_value(),
            },
        )
        assert response.status_code == 201
        ticket = response.json()
        lost = []

        def bridge(request):
            assert request.url.scheme == "https" and request.url.host == "scheduler.test"
            response = server.request(
                request.method, request.url.path, headers=request.headers, content=request.content
            )
            if request.url.path.endswith("/register") and not lost:
                assert response.status_code == 200
                lost.append(True)
                raise httpx.ReadError("Response was lost", request=request)
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        clients = []

        def public_http():
            client = httpx.Client(transport=httpx.MockTransport(bridge))
            clients.append(client)
            return client

        setup = ClusterSetup(
            root,
            authorize=lambda: None,
            http_factory=public_http,
            network_factory=lambda: TailscaleClient(runner=tailscale, executable="tailscale"),
        )
        with pytest.raises(httpx.ReadError):
            setup.init(
                config,
                scheduler="https://scheduler.test",
                enrollment_token=SecretStr(ticket["token"]),
            )
        assert (root / "enrollment.json").stat().st_mode & 0o777 == 0o600
        assert not (root / "headscale-join.key").exists()
        registered = setup.init(
            config, scheduler="https://scheduler.test", enrollment_token=SecretStr(ticket["token"])
        )
        assert str(registered.cluster_id) == ticket["cluster_id"]
        assert len([call for call in calls if call[1] == "up"]) == 1
        assert not (root / "enrollment.json").exists()
        assert not config_confirmed(root)
        assert load_config(root) == registered
        credentials = load_credentials(root / "credentials.json")
        assert credentials.scheduler_url == "https://agent.scheduler.test"
        assert credentials.agent_token.get_secret_value() not in (root / "cluster.yaml").read_text()
        with scheduler_db.transaction() as session:
            assert session.get(Cluster, registered.cluster_id).headscale_node_id == "1"
        response = server.post(
            f"/api/agents/{registered.cluster_id}/unregister",
            json={
                "schema_version": 1,
                "cluster": registered.model_dump(mode="json"),
                "state": "DESTROYED",
                "timestamp": utcnow().isoformat(),
                "boards": [
                    {"board_id": board.board_id, "state": "IDLE"}
                    for board in registered.boards
                    if board
                ],
                "jobs": [],
                "queues": {},
                "artifacts": [],
                "last_event_sequence": 0,
                "system": {"cpu": 0, "memory": 0, "disk_free": 1, "disk_total": 1},
            },
            headers={
                "Authorization": "Bearer " + credentials.agent_token.get_secret_value(),
            },
        )
        assert response.status_code == 202
        assert config.cluster_id is None
        assert len(clients) == 2 and all(client.is_closed for client in clients)


def test_concurrent_setup_cannot_replace_protected_receipt(tmp_path, config):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    lock = ExclusiveLock(root / "enrollment.lock")
    lock.acquire()
    try:
        with httpx.Client(
            transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP"))
        ) as client:
            setup = MacEnrollment(root, client, TailscaleClient(executable="tailscale"))
            with pytest.raises(PlatformError) as error:
                setup.enroll("https://scheduler.test", "secret-token", config)
            assert error.value.code == "ALREADY_OWNED"
            assert not (root / "enrollment.json").exists()
    finally:
        lock.release()


def test_enrollment_roles_and_scope_cannot_be_swapped(auth_stack, scheduler_db):
    sessions, login, directory, provider, _ = auth_stack
    network = MockNetwork()
    service = EnrollmentService(
        scheduler_db,
        network,
        "https://headscale.test",
        SecretStr(Fernet.generate_key().decode()),
        agent_origin="https://agent.scheduler.test",
    )
    app = create_app(
        scheduler_db,
        AuthAPI(sessions, login, "https://scheduler.test"),
        enrollment=service,
        maintenance=False,
    )
    user = sessions.issue_authorized(provider.identity)
    user_header = {"Authorization": "Bearer " + user.access_token.get_secret_value()}
    with TestClient(app, base_url="https://scheduler.test") as client:
        assert (
            client.post("/api/admin/enrollments", json={}, headers=user_header).status_code == 403
        )
        assert client.get("/api/admin/enrollments", headers=user_header).status_code == 403
        assert (
            client.post(
                "/api/enrollment/claim", json={"bootstrap_secret": "a" * 48}, headers=user_header
            ).status_code
            == 409
        )
        directory.members["admins"].add(provider.identity.email)
        ticket = client.post("/api/admin/enrollments", json={}, headers=user_header).json()
        inventory = client.get("/api/admin/enrollments", headers=user_header)
        assert ticket["token"] not in inventory.text and "token_hash" not in inventory.text
        bootstrap_header = {"Authorization": "Bearer " + ticket["token"]}
        assert client.get("/api/jobs", headers=bootstrap_header).status_code == 401
        assert (
            client.post(
                "/api/enrollment/claim",
                json={"bootstrap_secret": "a" * 48},
                headers=bootstrap_header,
            ).status_code
            == 200
        )
