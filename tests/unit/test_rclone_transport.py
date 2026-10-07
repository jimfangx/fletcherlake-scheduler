"""Rclone inherits no user remotes and keeps SSH identity, host pin and process limits."""

import asyncio
import csv
from datetime import timedelta
from pathlib import Path

import pytest
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_common.models.cluster import EnvironmentConfig
from fl_common.models.transfer import TransferEndpoint
from fl_common.process import CommandResult
from fl_common.rclone import Rclone
from fl_common.ssh import create_identity

from tests.transfer import grant


async def test_rclone_uses_pinned_openssh_and_bounded_pipeline(tmp_path, monkeypatch):
    parent = tmp_path / 'paths with spaces ü " and \\ chars'
    parent.mkdir()
    scope, identity = grant(parent, b"payload")
    endpoint = TransferEndpoint(
        host="gateway.example.edu", port=2222, host_key=create_identity(parent / "host")
    )
    monkeypatch.setenv("RCLONE_SFTP_SSH", "malicious-command")
    monkeypatch.setenv("RCLONE_RC_NO_AUTH", "true")

    async def runner(argv, *, timeout, env):  # noqa: ASYNC109 -- subprocess runner contract
        config = Path(argv[argv.index("--config") + 1])
        assert await asyncio.to_thread(config.read_bytes) == b""
        assert (await asyncio.to_thread(config.stat)).st_mode & 0o077 == 0
        ssh = next(csv.reader([argv[argv.index("--sftp-ssh") + 1]], delimiter=" "))
        assert ssh[0] == "ssh"
        assert ssh[ssh.index("-i") + 1] == str(identity)
        assert "-oStrictHostKeyChecking=yes" in ssh
        assert "-oGlobalKnownHostsFile=/dev/null" in ssh
        assert "-oHostKeyAlgorithms=ssh-ed25519" in ssh
        assert "-oIdentityAgent=none" in ssh
        known = Path(
            next(arg.split("=", 1)[1] for arg in ssh if arg.startswith("-oUserKnownHostsFile="))
        )
        assert (
            await asyncio.to_thread(known.read_text)
            == f"[gateway.example.edu]:2222 {endpoint.host_key}\n"
        )
        assert not any(k.startswith("RCLONE_") for k in env)
        assert "--inplace" in argv and "--ignore-times" in argv
        assert argv[argv.index("--sftp-concurrency") + 1] == "64"
        assert argv[-1] == f":sftp:/transfer/{scope.transfer_id}/binary"
        assert argv[-2] == str(parent / "local:input")
        assert 0 < timeout <= 45
        return CommandResult(tuple(argv), 0, b"", b"")

    await Rclone("rclone", runner=runner).copy(
        parent / "local:input", endpoint, scope, "binary", identity
    )


async def test_expired_scope_and_wrong_identity_never_start_process(tmp_path):
    scope, identity = grant(tmp_path, b"payload")
    endpoint = TransferEndpoint(
        host="gateway.example.edu", host_key=create_identity(tmp_path / "host")
    )

    async def runner(*args, **kwargs):
        pytest.fail("Invalid scope started rclone")

    transport = Rclone("rclone", runner=runner)
    scope.expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(PlatformError, match="expired"):
        await transport.copy(tmp_path / "input", endpoint, scope, "binary", identity)
    scope.expires_at = utcnow() + timedelta(seconds=30)
    bad = tmp_path / "wrong"
    create_identity(bad)
    with pytest.raises(PlatformError, match="differs"):
        await transport.copy(tmp_path / "input", endpoint, scope, "binary", bad)


async def test_cancellation_is_not_converted_into_transfer_failure(tmp_path):
    scope, identity = grant(tmp_path, b"payload")
    endpoint = TransferEndpoint(
        host="gateway.example.edu", host_key=create_identity(tmp_path / "host")
    )

    async def runner(*args, **kwargs):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await Rclone("rclone", runner=runner).copy(
            tmp_path / "input", endpoint, scope, "binary", identity
        )


def test_legacy_metadata_cannot_select_bbcp_or_open_data_ports(tmp_path):
    environment = EnvironmentConfig.model_validate({"bbcp": {"path": "/obsolete/bbcp"}})
    assert environment.rclone is None
    assert "bbcp" not in environment.model_dump()
    endpoint = TransferEndpoint.model_validate(
        {
            "host": "gateway.example.edu",
            "host_key": create_identity(tmp_path / "host"),
            "data_port_first": 5000,
            "data_port_last": 5099,
        }
    )
    assert "data_port_first" not in endpoint.model_dump()
    assert "data_port_last" not in endpoint.model_dump()
