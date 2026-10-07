"""Encrypted rclone/SFTP collateral IO with pinned SSH identity and a grant deadline."""

import asyncio
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID

from .errors import PlatformError
from .files import atomic_write
from .models.artifact import ArtifactKind
from .models.base import utcnow
from .models.transfer import TransferEndpoint, TransferGrant
from .process import CommandResult, run_command
from .ssh import identity_public_key

Runner = Callable[..., Awaitable[CommandResult]]


class Rclone:
    def __init__(self, executable: str | None = None, runner: Runner = run_command) -> None:
        selected = executable or shutil.which("rclone")
        if selected is None:
            raise PlatformError("RCLONE_MISSING", "Install rclone before transferring collateral")
        self.executable, self.runner = selected, runner

    async def copy(
        self,
        local: Path,
        endpoint: TransferEndpoint,
        grant: TransferGrant,
        kind: ArtifactKind,
        identity: Path,
    ) -> None:
        if kind not in {ref.kind for ref in grant.files}:
            raise PlatformError("TRANSFER_SCOPE", "Artifact is outside this transfer scope")
        remaining = (grant.expires_at - utcnow()).total_seconds()
        if remaining <= 0:
            raise PlatformError("TRANSFER_EXPIRED", "Transfer credentials expired")
        if await asyncio.to_thread(identity_public_key, identity) != grant.public_key:
            raise PlatformError("TRANSFER_IDENTITY", "Private key differs from transfer identity")
        with tempfile.TemporaryDirectory(prefix="fl-rclone-") as name:
            root = Path(name)
            known = root / "known_hosts"
            host = endpoint.host if endpoint.port == 22 else f"[{endpoint.host}]:{endpoint.port}"
            atomic_write(known, f"{host} {endpoint.host_key}\n".encode())
            # Use OpenSSH for the same identity/host-key policy on Linux and macOS.
            # Rclone parses --sftp-ssh as space-delimited CSV, not a shell.
            ssh = [
                "ssh",
                "-F",
                "/dev/null",
                "-oBatchMode=yes",
                "-oIdentitiesOnly=yes",
                "-oStrictHostKeyChecking=yes",
                "-oGlobalKnownHostsFile=/dev/null",
                "-oHostKeyAlgorithms=ssh-ed25519",
                "-oIdentityAgent=none",
                f"-oUserKnownHostsFile={known}",
                "-oServerAliveInterval=15",
                "-oServerAliveCountMax=2",
                "-p",
                str(endpoint.port),
                "-i",
                str(await asyncio.to_thread(identity.resolve)),
                "-l",
                endpoint.username,
                endpoint.host,
            ]
            config = root / "rclone.conf"
            atomic_write(config, b"")
            remote = self.remote_path(endpoint, grant.transfer_id, kind)
            local_name = str(await asyncio.to_thread(local.resolve))
            argv = [
                self.executable,
                "copyto",
                "--config",
                str(config),
                "--sftp-ssh",
                " ".join('"' + arg.replace('"', '""') + '"' for arg in ssh),
                "--sftp-shell-type",
                "unix",
                "--sftp-disable-hashcheck",
                "--sftp-set-modtime=false",
                # pkg/sftp caps the complete DATA response at 256 KiB, including headers.
                "--sftp-chunk-size",
                "240Ki",
                "--sftp-concurrency",
                "64",
                "--transfers",
                "1",
                "--checkers",
                "1",
                # SFTP pipelines many outstanding requests on an encrypted connection.
                # Avoid independent per-range sessions and temporary remote filenames:
                # the gateway exposes only exact manifest paths and publishes on close.
                "--multi-thread-streams",
                "0",
                "--inplace",
                "--ignore-times",
                "--retries",
                "1",
                "--low-level-retries",
                "1",
                "--contimeout",
                f"{min(30, remaining):.3f}s",
                "--timeout",
                f"{remaining:.3f}s",
                "--log-level",
                "ERROR",
            ]
            argv += [local_name, remote] if grant.direction == "upload" else [remote, local_name]
            # Do not inherit user remotes, passwords, RC endpoints or transfer overrides.
            environment = {k: v for k, v in os.environ.items() if not k.startswith("RCLONE_")}
            try:
                await self.runner(argv, timeout=remaining, env=environment)
            except PlatformError as error:
                raise PlatformError(
                    "TRANSFER_FAILED", "rclone collateral transfer failed"
                ) from error

    @staticmethod
    def remote_path(endpoint: TransferEndpoint, transfer_id: UUID, kind: ArtifactKind) -> str:
        return f":sftp:/transfer/{transfer_id}/{kind}"
