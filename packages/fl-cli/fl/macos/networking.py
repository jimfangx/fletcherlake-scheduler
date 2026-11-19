"""Mac-side Headscale join commands. Auth keys travel through protected files."""

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fl_common.errors import PlatformError
from fl_common.files import atomic_write
from fl_common.models.enrollment import EnrollmentGrant

CommandRunner = Callable[[list[str]], str]


def command_diagnostic(argv: list[str], stderr: str) -> str:
    for index, arg in enumerate(argv):
        value = (
            argv[index + 1]
            if arg == "--auth-key" and index + 1 < len(argv)
            else arg.removeprefix("--auth-key=")
            if arg.startswith("--auth-key=")
            else ""
        )
        if value.startswith("file:"):
            try:
                value = Path(value.removeprefix("file:")).read_text().strip()
            except OSError:
                value = ""
        if value:
            stderr = stderr.replace(value, "[redacted]")
    stderr = re.sub(r"(?:hskey-(?:auth|api)|tskey-[a-z]+)-[A-Za-z0-9_-]+", "[redacted]", stderr)
    return stderr.strip()[:4000]


def run_command(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        raise PlatformError(
            "NETWORK_JOIN_FAILED", "Tailscale command timed out after 120s"
        ) from None
    except OSError as error:
        raise PlatformError(
            "NETWORK_JOIN_FAILED", "Cannot start Tailscale: " + str(error)
        ) from None
    if result.returncode:
        diagnostic = command_diagnostic(argv, result.stderr)
        raise PlatformError(
            "NETWORK_JOIN_FAILED",
            "Tailscale command failed" + (": " + diagnostic if diagnostic else ""),
            exit_code=result.returncode,
        )
    return result.stdout


class TailscaleClient:
    def __init__(
        self, *, runner: CommandRunner = run_command, executable: str | None = None
    ) -> None:
        self.runner = runner
        selected = executable or shutil.which("tailscale")
        if selected is None:
            app = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
            if app.is_file():
                selected = str(app)
        if not selected:
            raise PlatformError(
                "TAILSCALE_MISSING", "Install and start Tailscale before cluster enrollment"
            )
        self.executable = selected

    def join(self, grant: EnrollmentGrant, state_root: Path) -> str:
        hostname = "fl-" + grant.cluster_id.hex
        try:
            status = json.loads(self.runner([self.executable, "status", "--json"]))
        except (PlatformError, ValueError):
            status = {}
        peer = status.get("Self") or {}
        if (
            status.get("BackendState") == "Running"
            and peer.get("HostName") == hostname
            and peer.get("PublicKey")
        ):
            # A join can succeed before the caller saves node_key (e.g. SSH disconnects).
            # Reuse that node; forcing auth again could consume an already-used join key.
            # The scheduler still checks that this node belongs to the grant's exact key.
            return str(peer["PublicKey"])
        key_path = state_root / "headscale-join.key"
        atomic_write(key_path, grant.auth_key.get_secret_value().encode())
        try:
            self.runner(
                [
                    self.executable,
                    "up",
                    "--reset",
                    "--force-reauth",
                    "--accept-risk=lose-ssh",
                    "--login-server",
                    grant.headscale_url,
                    "--auth-key",
                    "file:" + str(key_path),
                    "--hostname",
                    hostname,
                    "--accept-routes=false",
                    "--accept-dns=true",
                    "--timeout=60s",
                ]
            )
        finally:
            key_path.unlink(missing_ok=True)
        return self.node_key()

    def node_key(self) -> str:
        assert self.executable is not None
        status: dict[str, Any] = json.loads(self.runner([self.executable, "status", "--json"]))
        if status.get("BackendState") != "Running" or not status.get("Self", {}).get("PublicKey"):
            raise PlatformError("NODE_NOT_JOINED", "Tailscale is not running with a node identity")
        return str(status["Self"]["PublicKey"])

    def logout(self) -> None:
        assert self.executable is not None
        self.runner([self.executable, "logout"])
