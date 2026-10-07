"""SFTP speaks only a grant's virtual paths and publishes only verified complete bytes."""

import io
import struct
from datetime import timedelta

import pytest
from fl_common.models.base import utcnow
from fl_gateway.sftp import (
    CLOSE,
    DENIED,
    HANDLE,
    INIT,
    MAX_PACKET,
    OK,
    OPEN,
    READ,
    SFTP,
    STATUS,
    UNSUPPORTED,
    WRITE,
    Packet,
    string,
    u32,
)
from fl_gateway.store import GatewayStore
from fl_gateway.stream import receive

from tests.transfer import TOKEN_HASH, grant


def status(response):
    opcode, payload = response
    assert opcode == STATUS
    return Packet(payload).number()


def open_file(server, kind="binary", flags=26):
    result, payload = server.handle(
        OPEN, string(f"{server.prefix}/{kind}".encode()) + u32(flags) + u32(0)
    )
    assert result == HANDLE, result
    return Packet(payload).text()


def write(server, handle, offset, data):
    return server.handle(WRITE, string(handle) + struct.pack(">Q", offset) + string(data))


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../binary",
        "/transfer/other/binary",
        "/transfer/{id}/results",
        "/transfer/{id}/../binary",
        "/transfer/{id}/binary/../../control-secret",
        "/transfer/{id}/binary\0",
        "/transfer/{id}/binary\\outside",
    ],
)
def test_paths_never_select_unrelated_files(tmp_path, path):
    scope, _ = grant(tmp_path, b"content")
    store = GatewayStore(tmp_path / "gateway")
    store.register(scope)
    with SFTP(store, scope) as server:
        packet = string(path.format(id=scope.transfer_id).encode()) + u32(26) + u32(0)
        assert status(server.handle(OPEN, packet)) == DENIED
    assert not (store.root / "jobs").exists()


def test_pipelined_out_of_order_upload_is_atomic_and_write_only(tmp_path):
    scope, _ = grant(tmp_path, b"abcdef")
    store = GatewayStore(tmp_path / "gateway")
    store.register(scope)
    with SFTP(store, scope) as server:
        handle = open_file(server)
        assert status(write(server, handle, 3, b"def")) == OK
        assert not store.path(scope, "binary").exists()
        assert status(server.handle(READ, string(handle) + struct.pack(">Q", 0) + u32(6))) == DENIED
        assert status(write(server, handle, 0, b"abc")) == OK
        assert status(server.handle(CLOSE, string(handle))) == OK
    assert store.path(scope, "binary").read_bytes() == b"abcdef"
    store.verify(scope.transfer_id, TOKEN_HASH)
    with SFTP(store, scope) as server:
        assert (
            status(
                server.handle(OPEN, string(f"{server.prefix}/binary".encode()) + u32(26) + u32(0))
            )
            == DENIED
        )


@pytest.mark.parametrize("offset,data", [(0, b"abcdefg"), (6, b"g"), (2**63, b"x")])
def test_write_cannot_exceed_declared_size(tmp_path, offset, data):
    scope, _ = grant(tmp_path, b"abcdef")
    store = GatewayStore(tmp_path / "gateway")
    store.register(scope)
    with SFTP(store, scope) as server:
        handle = open_file(server)
        assert status(write(server, handle, offset, data)) == DENIED
        assert status(server.handle(CLOSE, string(handle))) == DENIED
    assert not store.path(scope, "binary").exists()
    assert not list(store.root.rglob(".partial-*"))


@pytest.mark.parametrize("finish", ["disconnect", "corrupt", "revoke", "expire"])
def test_failed_or_revoked_upload_never_publishes(tmp_path, monkeypatch, finish):
    scope, _ = grant(tmp_path, b"abcdef")
    store = GatewayStore(tmp_path / "gateway")
    store.register(scope)
    with SFTP(store, scope) as server:
        handle = open_file(server)
        assert (
            status(write(server, handle, 0, b"xxxxxx" if finish == "corrupt" else b"abcdef")) == OK
        )
        if finish == "revoke":
            store.revoke(scope.transfer_id)
        if finish == "expire":
            monkeypatch.setattr("fl_gateway.store.utcnow", lambda: utcnow() + timedelta(minutes=20))
        if finish != "disconnect":
            assert status(server.handle(CLOSE, string(handle))) == DENIED
    assert not store.path(scope, "binary").exists()
    assert not list(store.root.rglob(".partial-*"))
    with store.lock(scope.transfer_id):
        pass


def test_read_grant_cannot_write_or_read_other_artifacts(tmp_path):
    upload, _ = grant(tmp_path, b"abcdef")
    store = GatewayStore(tmp_path / "gateway")
    store.register(upload)
    receive(store, upload.transfer_id, "binary", io.BytesIO(b"abcdef"))
    store.verify(upload.transfer_id, TOKEN_HASH)
    download, _ = grant(tmp_path, b"abcdef", source=upload)
    store.register(download)
    with SFTP(store, download) as server:
        assert (
            status(
                server.handle(OPEN, string(f"{server.prefix}/binary".encode()) + u32(26) + u32(0))
            )
            == DENIED
        )
        handle = open_file(server, flags=1)
        assert status(write(server, handle, 0, b"xxxxxx")) == DENIED
        store.revoke(upload.transfer_id)
        assert status(server.handle(READ, string(handle) + struct.pack(">Q", 0) + u32(6))) == DENIED
    assert store.path(upload, "binary").read_bytes() == b"abcdef"


@pytest.mark.parametrize("opcode", [9, 10, 13, 15, 18, 19, 20, 200])
def test_mutation_links_and_extensions_are_unsupported(tmp_path, opcode):
    scope, _ = grant(tmp_path, b"abcdef")
    store = GatewayStore(tmp_path / "gateway")
    store.register(scope)
    with SFTP(store, scope) as server:
        assert status(server.handle(opcode, b"")) == UNSUPPORTED


@pytest.mark.parametrize(
    "wire", [u32(MAX_PACKET + 1), u32(4), u32(5) + bytes([INIT]) + u32(2), u32(5) + bytes([INIT])]
)
def test_malformed_frames_are_bounded_and_rejected(tmp_path, wire):
    scope, _ = grant(tmp_path, b"abcdef")
    store = GatewayStore(tmp_path / "gateway")
    store.register(scope)
    with SFTP(store, scope) as server, pytest.raises(ValueError):
        server.serve(io.BytesIO(wire), io.BytesIO())
