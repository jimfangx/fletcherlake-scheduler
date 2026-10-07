"""Bounded stream helpers for gateway integrity and retention acceptance."""

import hashlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.files import sha256_file
from fl_common.models.artifact import ArtifactKind

from .store import GatewayStore


def chunks(stream: BinaryIO) -> Iterator[bytes]:
    while chunk := stream.read(1024 * 1024):
        yield chunk


def receive(store: GatewayStore, transfer_id: UUID, kind: ArtifactKind, stream: BinaryIO) -> None:
    with store.lock(transfer_id):
        grant, state = store.lookup(transfer_id)
        if grant.direction != "upload" or state != "OPEN":
            raise PlatformError("TRANSFER_SCOPE", "This scope cannot receive artifact bytes")
        target = store.path(grant, kind)
        ref = next(ref for ref in grant.files if ref.kind == kind)
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".partial-", dir=target.parent)
        partial = Path(name)
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                for chunk in chunks(stream):
                    size += len(chunk)
                    if size > ref.size_bytes:
                        raise PlatformError(
                            "ARTIFACT_INTEGRITY", "Upload exceeds its declared size"
                        )
                    digest.update(chunk)
                    output.write(chunk)
                if (digest.hexdigest(), size) != (ref.sha256, ref.size_bytes):
                    raise PlatformError(
                        "ARTIFACT_INTEGRITY", "Received bytes do not match the manifest"
                    )
                output.flush()
                os.fsync(output.fileno())
            store.received(grant, ref, partial)
        finally:
            partial.unlink(missing_ok=True)


def send(store: GatewayStore, transfer_id: UUID, kind: ArtifactKind, stream: BinaryIO) -> None:
    with store.lock(transfer_id):
        grant, _ = store.lookup(transfer_id)
        if grant.direction != "download":
            raise PlatformError("TRANSFER_SCOPE", "This scope cannot read artifact bytes")
        source = store.source(grant)
        target = store.path(source, kind)
        ref = next((ref for ref in grant.files if ref.kind == kind), None)
        if ref is None or target.is_symlink() or not target.is_file():
            raise PlatformError("TRANSFER_SCOPE", "Artifact is outside this download scope")
        if sha256_file(target) != (ref.sha256, ref.size_bytes):
            raise PlatformError("ARTIFACT_INTEGRITY", "Verified gateway content changed")
        with target.open("rb") as input_file:
            for chunk in chunks(input_file):
                store.lookup(transfer_id)
                store.source(grant)
                stream.write(chunk)
        stream.flush()
