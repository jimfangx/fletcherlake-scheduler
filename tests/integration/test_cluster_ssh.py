"""Actual OpenSSH through HTTPS, plus the relay's authorization and revocation boundary."""

import asyncio
import getpass
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from fl_client.credentials import Credentials, CredentialStore
from fl_common.models.base import utcnow
from fl_common.ssh import create_identity
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.db.models import Cluster, Event
from fl_scheduler.registry import Registry
from fl_scheduler.service import create_app
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from tests.dashboard_https import certificate, serve_https
from tests.headscale import free_port


@pytest.fixture
def ssh_cluster(scheduler_db, config):
    registered = Registry(scheduler_db).register(config, "agent-token")
    with scheduler_db.transaction() as session:
        row = session.get(Cluster, registered.cluster_id)
        row.headscale_addresses = ["100.64.0.9"]
    return registered.cluster_id


@contextmanager
def echo_server():
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            while data := self.request.recv(65536):
                self.request.sendall(data)

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            thread.join(5)


def connector(port, calls):
    async def connect(host, destination_port):
        calls.append((host, destination_port))
        return await asyncio.open_connection("127.0.0.1", port)

    return connect


def test_ssh_roles_capability_scope_replay_and_revocation(auth_stack, ssh_cluster, scheduler_db):
    sessions, _, directory, provider, app = auth_stack
    tokens = sessions.issue_authorized(provider.identity)
    token = tokens.access_token.get_secret_value()
    headers = {"Authorization": "Bearer " + token}
    path = f"/api/clusters/{ssh_cluster}/ssh"
    calls = []
    with echo_server() as port, TestClient(app) as client:
        app.state.ssh.connector = connector(port, calls)
        app.state.ssh.check_interval = 0.05
        assert client.post(path).status_code == 401
        assert client.post(path, headers=headers).status_code == 403
        directory.members["operators"].add(provider.identity.email)
        ticket = client.post(path, headers=headers).json()
        ws_path = "/api/ssh/" + ticket["session_id"]
        capability = {"Authorization": "Bearer " + ticket["token"]}
        for bad_headers in (headers, capability | {"Origin": "https://scheduler.test"}):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(ws_path, headers=bad_headers):
                    pass
        assert not calls
        with client.websocket_connect(ws_path, headers=capability) as websocket:
            payload = bytes(range(256)) * 256
            websocket.send_bytes(payload)
            received = b""
            while len(received) < len(payload):
                received += websocket.receive_bytes()
            assert received == payload
            assert calls == [("100.64.0.9", 22)]
            # A refresh rotates access credentials but preserves the authenticated session.
            rotated = asyncio.run(sessions.refresh(tokens.refresh_token.get_secret_value()))
            token = rotated.access_token.get_secret_value()
            headers = {"Authorization": "Bearer " + token}
            time.sleep(0.08)
            websocket.send_bytes(b"after credential rotation")
            assert websocket.receive_bytes() == b"after credential rotation"
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(ws_path, headers=capability):
                    pass
            directory.members["operators"].clear()
            assert websocket.receive_json()["code"] == "FORBIDDEN"
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_bytes()
        directory.members["admins"].add(provider.identity.email)
        ticket = client.post(path, headers=headers).json()
        with client.websocket_connect(
            "/api/ssh/" + ticket["session_id"],
            headers={"Authorization": "Bearer " + ticket["token"]},
        ) as websocket:
            websocket.send_bytes(b"admin")
            assert websocket.receive_bytes() == b"admin"
            sessions.revoke(token)
            assert websocket.receive_json()["code"] == "UNAUTHENTICATED"
    with scheduler_db.transaction() as session:
        events = list(session.scalars(select(Event).where(Event.type.like("SSH_%"))))
        assert sum(event.type == "SSH_OPENED" for event in events) == 2
        assert {event.payload["reason"] for event in events} >= {"FORBIDDEN", "UNAUTHENTICATED"}
        assert token not in repr([event.payload for event in events])


def test_ssh_expired_tickets_limits_and_retired_destinations(auth_stack, ssh_cluster, scheduler_db):
    sessions, _, directory, provider, app = auth_stack
    directory.members["operators"].add(provider.identity.email)
    token = sessions.issue_authorized(provider.identity).access_token.get_secret_value()
    headers = {"Authorization": "Bearer " + token}
    path = f"/api/clusters/{ssh_cluster}/ssh"
    with TestClient(app) as client:
        ticket = client.post(path, headers=headers).json()
        pending = next(iter(app.state.ssh.tickets.values()))
        pending.expires_at = utcnow() - timedelta(seconds=1)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/api/ssh/" + ticket["session_id"],
                headers={"Authorization": "Bearer " + ticket["token"]},
            ):
                pass
        assert client.post(path, headers=headers).status_code == 200
        assert client.post(path, headers=headers).status_code == 200
        assert client.post(path, headers=headers).status_code == 429
        with scheduler_db.transaction() as session:
            session.get(Cluster, ssh_cluster).headscale_addresses = ["127.0.0.1", "169.254.169.254"]
        assert client.post(path, headers=headers).json()["code"] == "SSH_UNAVAILABLE"
        with scheduler_db.transaction() as session:
            row = session.get(Cluster, ssh_cluster)
            row.headscale_addresses = ["100.64.0.9"]
            row.state = "DESTROYED"
        assert client.post(path, headers=headers).json()["code"] == "SSH_UNAVAILABLE"


@pytest.fixture
def native_sshd(tmp_path):
    identity, host = tmp_path / "identity", tmp_path / "host"
    keys = tmp_path / "authorized_keys"
    keys.write_text(create_identity(identity) + "\n")
    host_public = create_identity(host)
    port = free_port()
    config = tmp_path / "sshd.conf"
    config.write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {host}\n"
        f"PidFile {tmp_path / 'sshd.pid'}\nUsePAM no\nPasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\nPubkeyAuthentication yes\nStrictModes no\n"
        f"AuthorizedKeysFile {json.dumps(str(keys))}\nAllowUsers {getpass.getuser()}\n"
        "AllowTcpForwarding no\nX11Forwarding no\nLogLevel ERROR\n"
    )
    with (tmp_path / "sshd.log").open("w+") as logs:
        process = subprocess.Popen(
            [shutil.which("sshd") or "/usr/sbin/sshd", "-D", "-e", "-f", str(config)],
            stdout=logs,
            stderr=logs,
        )
        try:
            time.sleep(0.15)
            assert process.poll() is None, "Disposable SSH server failed to start"
            yield port, identity, host_public
        finally:
            process.terminate()
            process.wait(timeout=5)
            logs.seek(0)
            print(logs.read())


def test_native_ssh_over_public_https(
    auth_stack, scheduler_db, ssh_cluster, native_sshd, tmp_path, monkeypatch
):
    sessions, login, directory, provider, _ = auth_stack
    directory.members["operators"].add(provider.identity.email)
    tokens = sessions.issue_authorized(provider.identity)
    port, identity, host_public = native_sshd
    certificate(tmp_path)
    calls = []

    def factory(origin):
        app = create_app(scheduler_db, AuthAPI(sessions, login, origin), maintenance=False)
        app.state.ssh.connector = connector(port, calls)
        return app

    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "server.pem"))
    with serve_https(factory, tmp_path) as origin:
        store = CredentialStore(tmp_path / "client" / "credentials.json")
        with store.locked():
            store.save(Credentials.from_response(origin, tokens.wire()))
        known = tmp_path / "known_hosts"
        known.write_text(f"fl-cluster-{ssh_cluster} {host_public}\n")
        args = [
            sys.executable,
            "-m",
            "fl_client",
            "ssh",
            str(ssh_cluster),
            "--user",
            getpass.getuser(),
            "--identity",
            str(identity),
            "--credentials",
            str(store.path),
            "--known-hosts",
            str(known),
        ]
        result = subprocess.run(
            args + ["--command", "printf 'hello through HTTPS'; exit 7"],
            capture_output=True,
            timeout=20,
            env=os.environ,
        )
        assert result.returncode == 7, result.stderr.decode()
        assert result.stdout == b"hello through HTTPS"
        assert calls == [("100.64.0.9", 22)]
        # A changed host key must fail before any remote command executes.
        known.write_text(f"fl-cluster-{ssh_cluster} {create_identity(tmp_path / 'wrong-host')}\n")
        result = subprocess.run(
            args + ["--command", "printf 'must not run'"],
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 255
        assert result.stdout == b""
        assert b"HOST IDENTIFICATION HAS CHANGED" in result.stderr
