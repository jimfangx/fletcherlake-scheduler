"""Independent public uploads must share gateway ports without crossing transfer scopes."""

import asyncio
import hashlib

import pytest
from fl_common.bbcp import BBCP
from fl_common.models import ArtifactRef
from fl_gateway.store import GatewayStore

from tests.transfer import TOKEN_HASH, grant


@pytest.mark.parametrize("data_port_last", [5007, 5099], ids=["eight-ports", "default-range"])
async def test_concurrent_public_uploads_keep_manifests_separate(
    bbcp_gateway, tmp_path, data_port_last
):
    original, endpoint, binary = bbcp_gateway
    store = GatewayStore(original.root, binary, data_port_last=data_port_last)
    endpoint = endpoint.model_copy(update={"data_port_last": data_port_last})
    uploads = []
    for index in range(8):
        elf = f"public ELF for job {index}\n".encode() * 16384
        bitstream = f"public FPGA for job {index}\n".encode() * 8192
        scope, identity = grant(tmp_path, elf)
        scope.files.append(
            ArtifactRef(
                kind="bitstream",
                sha256=hashlib.sha256(bitstream).hexdigest(),
                size_bytes=len(bitstream),
            )
        )
        files = []
        for kind, payload in (("binary", elf), ("bitstream", bitstream)):
            path = tmp_path / f"job-{index}-{kind}"
            path.write_bytes(payload)
            files.append((kind, path))
        store.register(scope)
        uploads.append((scope, identity, files))

    async def upload(scope, identity, files):
        for kind, path in files:
            await BBCP(str(binary)).copy(path, endpoint, scope, kind, identity)

    # Cancel and reap every sibling's native process group if any transfer fails.
    async with asyncio.TaskGroup() as tasks:
        for scope, identity, files in uploads:
            tasks.create_task(upload(scope, identity, files))
    for scope, _, files in uploads:
        assert store.verify(scope.transfer_id, TOKEN_HASH) == scope.files
        for kind, path in files:
            assert store.path(scope, kind).read_bytes() == path.read_bytes()
