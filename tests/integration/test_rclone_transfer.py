"""Real Rclone payload IO stays behind pinned SSH and immutable manifest scopes."""

import hashlib
import io
import os

import pytest
from fl_common.errors import PlatformError
from fl_common.rclone import Rclone
from fl_common.ssh import create_identity
from fl_gateway.store import GatewayStore
from fl_gateway.stream import receive, send

from tests.transfer import TOKEN_HASH, grant


async def test_real_rclone_upload_freeze_and_private_pull(rclone_gateway, tmp_path):
    store, endpoint, binary = rclone_gateway
    data = b"large collateral simulation\n" * 8192
    upload, identity = grant(tmp_path, data)
    store.register(upload)
    source = tmp_path / "source with spaces.elf"
    source.write_bytes(data)
    transport = Rclone(str(binary))
    await transport.copy(source, endpoint, upload, "binary", identity)
    assert store.path(upload, "binary").read_bytes() == data
    assert store.verify(upload.transfer_id, TOKEN_HASH) == upload.files
    with pytest.raises(PlatformError) as error:
        await transport.copy(source, endpoint, upload, "binary", identity)
    assert error.value.code == "TRANSFER_FAILED"
    download, agent_identity = grant(tmp_path, data, source=upload)
    store.register(download)
    destination = tmp_path / "agent input.elf"
    await transport.copy(destination, endpoint, download, "binary", agent_identity)
    assert destination.read_bytes() == data
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == upload.files[0].sha256


async def test_real_rclone_rejects_corrupt_bytes_and_wrong_host(rclone_gateway, tmp_path):
    store, endpoint, binary = rclone_gateway
    upload, identity = grant(tmp_path, b"expected bytes")
    store.register(upload)
    source = tmp_path / "corrupt.elf"
    source.write_bytes(b"incorrect bytes")
    transport = Rclone(str(binary))
    with pytest.raises(PlatformError) as error:
        await transport.copy(source, endpoint, upload, "binary", identity)
    assert error.value.code == "TRANSFER_FAILED"
    assert not store.path(upload, "binary").exists()
    assert not list(store.path(upload, "binary").parent.glob(".partial-*"))
    wrong_host = endpoint.model_copy(update={"host_key": create_identity(tmp_path / "wrong-host")})
    with pytest.raises(PlatformError) as error:
        await transport.copy(source, wrong_host, upload, "binary", identity)
    assert error.value.code == "TRANSFER_FAILED"


def test_gateway_stream_checks_before_publication_and_download(tmp_path):
    store = GatewayStore(tmp_path / "gateway")
    upload, _ = grant(tmp_path, b"expected bytes")
    store.register(upload)
    for data in (b"short", b"wrong content!", b"expected bytes and excess"):
        with pytest.raises(PlatformError) as error:
            receive(store, upload.transfer_id, "binary", io.BytesIO(data))
        assert error.value.code == "ARTIFACT_INTEGRITY"
        assert not store.path(upload, "binary").exists()
    receive(store, upload.transfer_id, "binary", io.BytesIO(b"expected bytes"))
    store.verify(upload.transfer_id, TOKEN_HASH)
    download, _ = grant(tmp_path, b"expected bytes", source=upload)
    store.register(download)
    store.path(upload, "binary").write_bytes(b"tampered bytes")
    output = io.BytesIO()
    with pytest.raises(PlatformError) as error:
        send(store, download.transfer_id, "binary", output)
    assert error.value.code == "ARTIFACT_INTEGRITY"
    assert output.getvalue() == b""


@pytest.mark.parametrize("size", [0, 64 * 1024 * 1024 + 17], ids=["empty", "64-MiB"])
async def test_native_rclone_round_trip_across_full_packet_windows(rclone_gateway, tmp_path, size):
    store, endpoint, binary = rclone_gateway
    # Exceed the request window and end with a short block; random bytes avoid compression.
    payload = os.urandom(size)
    identity_root = tmp_path / 'identity space ü " and \\ chars'
    identity_root.mkdir()
    upload, identity = grant(identity_root, payload)
    store.register(upload)
    source = tmp_path / "large input"
    source.write_bytes(payload)
    transport = Rclone(str(binary))
    await transport.copy(source, endpoint, upload, "binary", identity)
    assert store.verify(upload.transfer_id, TOKEN_HASH) == upload.files
    download, identity = grant(identity_root, payload, source=upload)
    store.register(download)
    target = tmp_path / "large output"
    await transport.copy(target, endpoint, download, "binary", identity)
    assert target.read_bytes() == payload
