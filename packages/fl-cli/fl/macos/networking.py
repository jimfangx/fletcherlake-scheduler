"""Mac-side Headscale join commands. Auth keys travel through protected files."""

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fl_common.errors import PlatformError
from fl_common.files import atomic_write
from fl_common.models.enrollment import EnrollmentGrant

CommandRunner = Callable[[list[str]], str]


def run_command(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise PlatformError(
            "NETWORK_JOIN_FAILED", "Tailscale command failed", exit_code=result.returncode
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
        key_path = state_root / "headscale-join.key"
        atomic_write(key_path, grant.auth_key.get_secret_value().encode())
        try:
            self.runner(
                [
                    self.executable,
                    "up",
                    "--login-server",
                    grant.headscale_url,
                    "--auth-key",
                    "file:" + str(key_path),
                    "--hostname",
                    "fl-" + grant.cluster_id.hex,
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
