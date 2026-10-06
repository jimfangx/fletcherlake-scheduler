"""Gateway authority boundaries, revocation races, and crash-retryable retention."""

import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_gateway.api import create_app
from fl_gateway.maintenance import sweep
from fl_gateway.ssh import request
from fl_gateway.store import GatewayStore
from fl_gateway.stream import receive
from pydantic import SecretStr

from tests.transfer import TOKEN, TOKEN_HASH, grant

CONTROL = "different protected control credential of sufficient length"


def test_control_is_separate_from_upload_verification(tmp_path):
    store = GatewayStore(tmp_path / "gateway", tmp_path / "bbcp")
    scope, _ = grant(tmp_path, b"expected bytes")
    with TestClient(create_app(store, SecretStr(CONTROL))) as client:
        payload = scope.model_dump(mode="json")
        upload_auth = {"Authorization": f"Bearer {TOKEN}"}
        control_auth = {"Authorization": f"Bearer {CONTROL}"}
        assert client.put("/internal/transfers", json=payload).status_code == 401
        assert (
            client.put("/internal/transfers", json=payload, headers=upload_auth).status_code == 401
        )
        assert (
            client.put("/internal/transfers", json=payload, headers=control_auth).status_code == 204
        )
        assert (
            client.put("/internal/transfers", json=payload, headers=control_auth).status_code == 204
        )
        path = f"/api/uploads/{scope.transfer_id}/verify"
        assert client.post(path, headers=control_auth).status_code == 401
        assert client.post(path, headers=upload_auth).status_code == 409
        receive(store, scope.transfer_id, "binary", io.BytesIO(b"expected bytes"))
        assert client.post(path, headers=upload_auth).json() == [
            ref.model_dump(mode="json") for ref in scope.files
        ]
        assert (
            client.delete(
                f"/internal/transfers/{scope.transfer_id}", headers=upload_auth
            ).status_code
            == 401
        )
        assert (
            client.delete(
                f"/internal/transfers/{scope.transfer_id}", headers=control_auth
            ).status_code
            == 204
        )
        assert (
            client.get(f"/internal/transfers/{scope.transfer_id}", headers=control_auth).json()[
                "state"
            ]
            == "REVOKED"
        )
        assert client.post(path, headers=upload_auth).status_code == 409


def test_revocation_during_receive_fences_publication(tmp_path):
    store = GatewayStore(tmp_path / "gateway", tmp_path / "bbcp")
    scope, _ = grant(tmp_path, b"expected bytes")
    store.register(scope)

    class RevokingInput(io.BytesIO):
        def read(self, size=-1):
            store.revoke(scope.transfer_id)
            return super().read(size)

    with pytest.raises(PlatformError) as error:
        receive(store, scope.transfer_id, "binary", RevokingInput(b"expected bytes"))
    assert error.value.code == "TRANSFER_REVOKED"
    assert not store.path(scope, "binary").exists()
    assert not list(store.path(scope, "binary").parent.glob(".partial-*"))


@pytest.mark.parametrize("already_verified", [False, True])
def test_revocation_during_verification_cannot_resurrect_upload(
    tmp_path, monkeypatch, already_verified
):
    store = GatewayStore(tmp_path / "gateway", tmp_path / "bbcp")
    scope, _ = grant(tmp_path, b"expected bytes")
    store.register(scope)
    receive(store, scope.transfer_id, "binary", io.BytesIO(b"expected bytes"))
    if already_verified:
        store.verify(scope.transfer_id, TOKEN_HASH)

    def revoked_checksum(path):
        store.revoke(scope.transfer_id)
        return scope.files[0].sha256, scope.files[0].size_bytes

    monkeypatch.setattr("fl_gateway.store.sha256_file", revoked_checksum)
    with pytest.raises(PlatformError) as error:
        store.verify(scope.transfer_id, TOKEN_HASH)
    assert error.value.code == "TRANSFER_REVOKED"
    assert store.lookup(scope.transfer_id, active=False)[1] == "REVOKED"


def test_expired_retention_is_durable_and_deletion_retries(tmp_path, monkeypatch):
    store = GatewayStore(tmp_path / "gateway", tmp_path / "bbcp")
    scope, _ = grant(tmp_path, b"expected bytes")
    store.register(scope)
    receive(store, scope.transfer_id, "binary", io.BytesIO(b"expected bytes"))
    future = utcnow() + timedelta(hours=1)
    monkeypatch.setattr("fl_gateway.maintenance.utcnow", lambda: future)
    monkeypatch.setattr("fl_gateway.authorized_keys.utcnow", lambda: future)
    with store.lock(scope.transfer_id):
        sweep(store)
        assert store.lookup(scope.transfer_id, active=False)[1] == "REVOKED"
        assert store.path(scope, "binary").exists()
        assert (store.root / "authorized_keys").read_text() == ""
    sweep(GatewayStore(store.root, store.bbcp))
    assert not store.path(scope, "binary").exists()


@pytest.mark.parametrize(
    "command,options,path",
    [
        ("sh", "", "binary"),
        ("bbcp SNK", " -C /etc/passwd", "binary"),
        ("bbcp SNK", " -e /bin/sh", "binary"),
        ("bbcp SNK", " -z", "binary"),
        ("bbcp SNK", " -Y " + "b" * 64, "binary"),
        ("bbcp SNK", "", "../../outside"),
        ("bbcp SNK", "", "results"),
    ],
)
def test_forced_command_rejects_shell_options_and_paths(tmp_path, command, options, path):
    scope, _ = grant(tmp_path, b"expected bytes")
    wire = (
        f"-n -N o -s 4 -Y {'a' * 64} -H none:0{options}\n/transfer/{scope.transfer_id}/{path}\n\0"
    ).encode()
    with pytest.raises(PlatformError):
        request(io.BytesIO(wire), scope, command)
