"""BBCP collateral transport with pinned SSH host identity and an external process deadline."""

import asyncio
import math
import os
import secrets
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


class BBCP:
    def __init__(self, executable: str | None = None, runner: Runner = run_command) -> None:
        selected = executable or shutil.which("bbcp")
        if selected is None:
            raise PlatformError("BBCP_MISSING", "Install BBCP before transferring collateral")
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
        with tempfile.TemporaryDirectory(prefix="fl-bbcp-", dir="/tmp") as name:
            root = Path(name)
            known = root / "known_hosts"
            hostname = (
                endpoint.host if endpoint.port == 22 else f"[{endpoint.host}]:{endpoint.port}"
            )
            atomic_write(known, f"{hostname} {endpoint.host_key}\n".encode())
            config = root / "options"
            # BBCP's default data-session token is only 32 bits. Supply 256 bits through
            # a protected config file, never process arguments or job metadata.
            atomic_write(config, f"-Y {secrets.token_hex(32)}\n".encode())
            remote = self.remote_path(endpoint, grant.transfer_id, kind)
            upload = grant.direction == "upload"
            command = (
                "ssh -F /dev/null -oBatchMode=yes -oIdentitiesOnly=yes "
                f"-oStrictHostKeyChecking=yes -oUserKnownHostsFile={known} "
                f"-p {endpoint.port} %I -l %U %H bbcp"
            )
            argv = [
                self.executable,
                "-C",
                str(config),
                "-n",
                "-N",
                "o" if upload else "i",
                "-s",
                "4",
                "-t",
                str(max(1, math.floor(remaining))),
                "-Z",
                f"{endpoint.data_port_first}:{endpoint.data_port_last}",
                "-i",
                str(identity),
                "-T" if upload else "-S",
                command,
            ]
            if not upload:
                argv += ["-z", "-f"]
            argv += [str(local), remote] if upload else [remote, str(local)]
            environment = {**os.environ, "bbcp_CONFIGFN": str(config)}
            try:
                await self.runner(argv, timeout=remaining, env=environment)
            except PlatformError as error:
                raise PlatformError("TRANSFER_FAILED", "BBCP collateral transfer failed") from error

    @staticmethod
    def remote_path(endpoint: TransferEndpoint, transfer_id: UUID, kind: ArtifactKind) -> str:
        host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
        return f"{endpoint.username}@{host}:/transfer/{transfer_id}/{kind}"
