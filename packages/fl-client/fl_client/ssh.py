"""Native OpenSSH over a short-lived, authenticated scheduler WebSocket."""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.files import atomic_write
from fl_common.models.base import Schema, utcnow
from fl_common.network import https_origin
from pydantic import AwareDatetime, SecretStr, field_validator
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from .api import RemoteClient
from .credentials import CredentialStore


class SshTicket(Schema):
    scheduler: str
    session_id: UUID
    token: SecretStr
    expires_at: AwareDatetime

    @field_validator("scheduler")
    @classmethod
    def valid_scheduler(cls, value: str) -> str:
        return https_origin(value)

    def protected_bytes(self) -> bytes:
        body = self.model_dump(mode="json")
        body["token"] = self.token.get_secret_value()
        return json.dumps(body).encode()


def open_ssh(
    client: RemoteClient,
    cluster_id: UUID,
    *,
    user: str,
    identity: Path | None = None,
    known_hosts: Path | None = None,
    command: str | None = None,
) -> int:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user):
        raise PlatformError("SSH_USER", "Provide a valid Mac SSH username")
    ticket = SshTicket(
        scheduler=client.origin(), **client.request("POST", f"/api/clusters/{cluster_id}/ssh")
    )
    with tempfile.TemporaryDirectory(prefix="fl-ssh-") as name:
        path = Path(name) / "ticket.json"
        atomic_write(path, ticket.protected_bytes())
        # ProxyCommand uses a shell and OpenSSH expands percent tokens before execution.
        proxy = shlex.join(
            arg.replace("%", "%%") for arg in (sys.executable, "-m", "fl_client.ssh", str(path))
        )
        alias = f"fl-cluster-{cluster_id}"
        argv = [
            "ssh",
            "-oProxyCommand=" + proxy,
            "-oHostKeyAlias=" + alias,
            "-oStrictHostKeyChecking=ask",
            "-oClearAllForwardings=yes",
            "-oForwardAgent=no",
            "-oServerAliveInterval=30",
            "-oServerAliveCountMax=3",
            "-oConnectTimeout=35",
            "-l",
            user,
        ]
        if identity is not None:
            argv += ["-i", str(identity.expanduser().resolve())]
        if known_hosts is not None:
            path_value = str(known_hosts.expanduser().resolve()).replace("%", "%%")
            argv += ["-oUserKnownHostsFile=" + '"' + path_value.replace('"', '\\"') + '"']
        argv.append(alias)
        if command is not None:
            argv.append(command)
        # Inherit the terminal for passwords, host-key confirmation and interactive shells.
        return subprocess.call(argv)


def proxy(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        CredentialStore._protected(os.fstat(descriptor))
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            ticket = SshTicket.model_validate_json(handle.read(8192))
    finally:
        os.close(descriptor)
    if ticket.expires_at <= utcnow():
        raise PlatformError("SSH_EXPIRED", "SSH ticket expired; run the command again")
    url = "wss" + ticket.scheduler[5:] + f"/api/ssh/{ticket.session_id}"
    with connect(
        url,
        additional_headers={"Authorization": "Bearer " + ticket.token.get_secret_value()},
        compression=None,
        max_size=65536,
        open_timeout=20,
        close_timeout=2,
    ) as websocket:

        def send() -> None:
            try:
                while data := os.read(0, 65536):
                    websocket.send(data)
            except (OSError, ConnectionClosed):
                pass
            finally:
                websocket.close()

        # OpenSSH closes these pipes when it exits. A daemon reader cannot hold up shutdown.
        threading.Thread(target=send, daemon=True).start()
        for data in websocket:
            if isinstance(data, str):
                body = json.loads(data)
                raise PlatformError("SSH_FAILED", str(body.get("message", "SSH tunnel closed")))
            remaining = memoryview(data)
            while remaining:
                remaining = remaining[os.write(1, remaining) :]
    return 0


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("SSH proxy requires a protected ticket file")
        return proxy(Path(sys.argv[1]))
    except InvalidStatus as error:
        status = error.response.status_code
        hint = {
            400: "check the scheduler WebSocket proxy configuration",
            403: "SSH ticket rejected; retry and check operator/admin access",
            404: "scheduler SSH WebSocket route is missing; update its nginx configuration",
            426: "check the scheduler WebSocket Upgrade headers",
            502: "scheduler SSH service is unavailable",
            503: "scheduler SSH service is unavailable",
        }.get(status, "check the scheduler SSH service")
        print(f"SSH tunnel rejected (HTTP {status}): {hint}", file=sys.stderr)
        return 1
    except (PlatformError, OSError, ValueError, ConnectionClosed) as error:
        message = error.message if isinstance(error, PlatformError) else type(error).__name__
        print("SSH tunnel: " + message, file=sys.stderr)
        return 1
    except Exception as error:
        # WebSocket handshake failures must not dump credential-bearing request headers.
        print("SSH tunnel could not connect (" + type(error).__name__ + ")", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
