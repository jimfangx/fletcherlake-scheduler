"""Public BBCP upload, private encrypted fetch, source fencing and actual mock execution."""

import asyncio
import hashlib
from datetime import timedelta

import pytest
from fl_agent.commands import CommandHandler
from fl_common.bbcp import BBCP
from fl_common.errors import PlatformError
from fl_common.models import ArtifactRef, JobConfig, JobSpec
from fl_common.models.base import utcnow
from fl_common.protocol import Message, MessageType
from fl_common.protocol.delivery import FetchCommand, StageReceipt
from fl_gateway.store import GatewayStore

from tests.network_sockets import SocketAdapters
from tests.private_peers import enrolled, ready_ssh
from tests.private_ssh import private_ssh
from tests.transfer import TOKEN_HASH, grant


@pytest.mark.parametrize(
    "tailscale_peers", [False, True], indirect=True, ids=["normal", "derp-only"]
)
async def test_public_upload_private_fetch_and_source_fence(
    headscale_server, tailscale_peers, bbcp_gateway, service_factory, tmp_path
):
    coordinator, api_key = headscale_server
    store, public, binary = bbcp_gateway
    adapters = SocketAdapters(tmp_path)
    store = GatewayStore(store.root, binary, data_port_last=5007)
    public = public.model_copy(update={"data_port_last": 5007})
    peers, addresses = enrolled(
        coordinator,
        api_key,
        tailscale_peers,
        {
            "mac": "tag:cluster",
            "gateway": "tag:transfer-gateway",
            "other-mac": "tag:cluster",
        },
    )
    cluster, gateway, other = (peers[name] for name in ("mac", "gateway", "other-mac"))
    cluster_ip, gateway_ip, other_ip = (addresses[name] for name in ("mac", "gateway", "other-mac"))
    # The bridge preserves the real peer address in OpenSSH's native from= check.
    # Payload forwarding is opaque: every byte still traverses real WireGuard peers.
    with private_ssh(tmp_path, adapters, gateway_ip, tmp_path / "sshd.conf") as (port, sources):
        gateway.command(
            "serve", "--bg", "--tcp=22", "--proxy-protocol=1", f"tcp://127.0.0.1:{port}"
        )
        for data_port in range(5000, 5008):
            gateway.command("serve", "--bg", f"--tcp={data_port}", f"tcp://127.0.0.1:{data_port}")
        endpoint = public.model_copy(update={"host": gateway_ip, "port": 22})
        ready_ssh(gateway_ip, [cluster, other])
        data = b"encrypted private ELF payload\n" * 16384
        bit_data = b"encrypted private FPGA payload\n" * 8192
        upload, identity = grant(tmp_path, data)
        upload.files.append(
            ArtifactRef(
                kind="bitstream",
                sha256=hashlib.sha256(bit_data).hexdigest(),
                size_bytes=len(bit_data),
            )
        )
        upload.expires_at = utcnow() + timedelta(minutes=2)
        spec = JobSpec.from_config(
            JobConfig(binary="input.elf", bitstream="fpga.bit"),
            "alice@berkeley.edu",
            binary=upload.files[0],
            bitstream=upload.files[1],
        )
        upload.job_id = spec.job_id
        store.register(upload)
        for kind, payload in (("binary", data), ("bitstream", bit_data)):
            path = tmp_path / f"public {kind}"
            path.write_bytes(payload)
            # This client uses ordinary public SSH/BBCP and no Headscale proxy.
            await BBCP(str(binary)).copy(path, public, upload, kind, identity)
        assert store.verify(upload.transfer_id, TOKEN_HASH) == upload.files
        service = service_factory()
        await service.start()
        download, _ = grant(tmp_path, data, source=upload)
        download.source_networks = [f"{cluster_ip}/32"]
        download.expires_at = utcnow() + timedelta(minutes=2)
        handler = CommandHandler(service)
        staged = await handler.handle(
            Message(
                type=MessageType.JOB_STAGE,
                payload={
                    "spec": spec.model_dump(mode="json"),
                    "board_id": "board-0",
                    "transfer_id": str(download.transfer_id),
                },
            )
        )
        assert staged.accepted
        download.public_key = StageReceipt.model_validate(staged.result).public_key
        store.register(download)
        agent_identity = service.transfers.identity(spec.job_id, download.transfer_id)
        trace = tmp_path / "cluster-connect.log"
        service.transfers.transport = BBCP(str(binary), runner=adapters.runner(cluster, trace))
        fetched = await handler.handle(
            Message(
                type=MessageType.JOB_FETCH,
                payload=FetchCommand(
                    job_id=spec.job_id,
                    endpoint=endpoint,
                    grant=download,
                ).model_dump(mode="json"),
            )
        )
        assert fetched.accepted, fetched.error
        for kind, payload in (("binary", data), ("bitstream", bit_data)):
            assert service.store.path(spec.job_id, kind).read_bytes() == payload
        assert cluster_ip in sources
        connections = [line.split() for line in trace.read_text().splitlines()]
        assert sum(int(port) == 22 for _, port in connections) == 2
        assert sum(5000 <= int(port) < 5008 for _, port in connections) >= 8
        assert all(host == gateway_ip for host, _ in connections)
        cluster.assert_relay(gateway_ip)
        # The same correct key is denied from another permitted cluster by its /32.
        previous_connections = sources.count(other_ip)
        with pytest.raises(PlatformError) as error:
            await BBCP(str(binary), runner=adapters.runner(other, tmp_path / "other.log")).copy(
                tmp_path / "unauthorized", endpoint, download, "binary", agent_identity
            )
        assert error.value.code == "TRANSFER_FAILED"
        assert sources.count(other_ip) > previous_connections
        assert "Permission denied (publickey)" in (tmp_path / "other.error").read_text()
        assert not (tmp_path / "unauthorized").exists()
        assert (
            await handler.handle(
                Message(
                    type=MessageType.JOB_ENQUEUE,
                    payload={
                        "spec": spec.model_dump(mode="json"),
                        "board_id": "board-0",
                    },
                )
            )
        ).accepted
        async with asyncio.timeout(5):
            while not service.db.get(spec.job_id).state.terminal:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert service.db.get(spec.job_id).state == "SUCCEEDED"
        assert (
            sum(event.type == "JOB_INPUTS_VERIFIED" for event in service.db.job_events(spec.job_id))
            == 1
        )
