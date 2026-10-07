"""Public read authority is explicit; revocation interrupts a gateway sender between chunks."""

import io
from uuid import uuid4

import pytest
from fl_common.errors import PlatformError
from fl_common.models.transfer import TransferGrant
from fl_common.protocol.delivery import FetchCommand
from fl_gateway.store import GatewayStore
from fl_gateway.stream import receive, send
from pydantic import ValidationError

from tests.transfer import TOKEN, TOKEN_HASH, grant


def test_explicit_public_read_cannot_be_used_as_a_mac_fetch(tmp_path):
    from fl_common.models.transfer import TransferEndpoint

    source, _ = grant(tmp_path, b"read bytes")
    body = source.model_dump()
    body.update(direction="download", source_id=source.transfer_id, transfer_id=uuid4())
    with pytest.raises(ValidationError):
        TransferGrant.model_validate(body)
    public = TransferGrant.model_validate({**body, "public_download": True})
    with pytest.raises(ValidationError):
        FetchCommand(
            job_id=public.job_id,
            endpoint=TransferEndpoint(host="transfer.test", host_key=public.public_key),
            grant=public,
        )


def test_gateway_revocation_stops_an_existing_public_sender(tmp_path):
    from fl_common.ssh import create_identity

    data = b"payload bytes\n" * 200000
    source, _ = grant(tmp_path, data)
    store = GatewayStore(tmp_path / "gateway")
    store.register(source)
    receive(store, source.transfer_id, "binary", io.BytesIO(data))
    store.verify(source.transfer_id, TOKEN_HASH)
    download = TransferGrant.model_validate(
        {
            **source.model_dump(),
            "transfer_id": uuid4(),
            "direction": "download",
            "source_id": source.transfer_id,
            "public_download": True,
            "public_key": create_identity(tmp_path / "reader"),
        }
    )
    store.register(download)

    class RevokeOnFirstChunk(io.BytesIO):
        def write(self, data):
            result = super().write(data)
            store.revoke(download.transfer_id)
            return result

    destination = RevokeOnFirstChunk()
    with pytest.raises(PlatformError) as error:
        send(store, download.transfer_id, "binary", destination)
    assert error.value.code == "TRANSFER_EXPIRED"
    assert 0 < len(destination.getvalue()) < len(data)


def test_private_mac_publication_can_only_be_sealed_through_control_api(tmp_path):
    from fastapi.testclient import TestClient
    from fl_gateway.api import create_app
    from pydantic import SecretStr

    source, _ = grant(tmp_path, b"private publication")
    source.source_networks = ["127.0.0.1/32"]
    store = GatewayStore(tmp_path / "gateway")
    store.register(source)
    receive(store, source.transfer_id, "binary", io.BytesIO(b"private publication"))
    control = "separate scheduler control credential for sealing"
    with TestClient(create_app(store, SecretStr(control))) as client:
        public = client.post(
            f"/api/uploads/{source.transfer_id}/verify",
            headers={"Authorization": "Bearer " + TOKEN},
        )
        assert public.status_code == 401
        private = f"/internal/transfers/{source.transfer_id}/verify"
        assert client.post(private, headers={"Authorization": "Bearer " + TOKEN}).status_code == 401
        assert (
            client.post(private, headers={"Authorization": "Bearer " + control}).status_code == 200
        )
        assert store.lookup(source.transfer_id)[1] == "VERIFIED"
