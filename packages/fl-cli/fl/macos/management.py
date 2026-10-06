"""Native lifecycle orchestration; hardware effects always go through the local daemon."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fl_agent.configuration import (
    DEFAULT_RUNTIME_ROOT,
    DEFAULT_STATE_ROOT,
    invalidate_config,
    load_config,
    write_config,
)
from fl_common.errors import PlatformError
from fl_common.models import ClusterConfig, ClusterState
from fl_common.models.scheduler import ClusterSnapshot
from pydantic import SecretStr

from ..sdk import Cluster
from .destroy import destroy
from .enrollment import MacEnrollment, set_unregister_origin
from .inventory import InventoryInput, inventory
from .launchd import Installation
from .networking import TailscaleClient
from .provisioning import LABEL, Probe, Runner, install_agent, loaded, require_admin, run
from .readiness import wait_ready
from .retirement import management_lock, require_no_retirement


def public_client() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=False)


class ClusterSetup:
    """macOS management entrypoint that also works before the agent is installed.

    Injectable clients/runners support contract tests; production defaults retain macOS
    privilege and launchd behavior. Library methods never prompt or invoke the fl CLI.
    """

    def __init__(
        self,
        state_root: Path = DEFAULT_STATE_ROOT,
        runtime_root: Path = DEFAULT_RUNTIME_ROOT,
        *,
        runner: Runner = run,
        launchd_root: Path = Path("/Library/LaunchDaemons"),
        pixi: str | None = None,
        authorize: Callable[[], None] = require_admin,
        client_factory: Callable[[Path], Cluster] = Cluster.local,
        http_factory: Callable[[], httpx.Client] = public_client,
        network_factory: Callable[[], TailscaleClient] = TailscaleClient,
        probe: Probe = loaded,
    ) -> None:
        self.state_root, self.runtime_root = state_root.resolve(), runtime_root.resolve()
        self.runner, self.launchd_root, self.pixi = runner, launchd_root.resolve(), pixi
        self.authorize, self.client_factory = authorize, client_factory
        self.http_factory, self.network_factory = http_factory, network_factory
        self.probe = probe

    @property
    def socket(self) -> Path:
        return self.runtime_root / "agent.sock"

    def init(
        self,
        config: InventoryInput,
        *,
        scheduler: str,
        enrollment_token: str | SecretStr,
    ) -> ClusterConfig:
        self.authorize()
        with management_lock(self.state_root):
            require_no_retirement(self.state_root)
            if (self.state_root / "agent.db").exists() and not (
                self.state_root / "credentials.json"
            ).exists():
                raise PlatformError(
                    "STATE_NOT_EMPTY", "Archive prior agent history before a new enrollment"
                )
            if (self.state_root / "cluster.sha256").exists():
                raise PlatformError("CLUSTER_CONFIGURED", "Cluster is configured; use reconfigure")
            resolved = inventory(config)
            token = (
                enrollment_token.get_secret_value()
                if isinstance(enrollment_token, SecretStr)
                else enrollment_token
            )
            with self.http_factory() as http:
                return MacEnrollment(self.state_root, http, self.network_factory()).enroll(
                    scheduler, token, resolved
                )

    def confirm(self, project: Path, *, timeout: float = 90) -> dict[str, Any]:
        self.authorize()
        with management_lock(self.state_root):
            require_no_retirement(self.state_root)
            installed = (self.launchd_root / f"{LABEL}.plist").exists()
            self._install(project, installed=installed)
            return wait_ready(self.socket, timeout=timeout, client_factory=self.client_factory)

    def _install(self, project: Path, *, installed: bool) -> None:
        if installed:
            Installation.from_agent(
                self.launchd_root / f"{LABEL}.plist", self.state_root, self.runtime_root
            )
        install_agent(
            project.resolve(),
            self.state_root,
            self.runtime_root,
            runner=self.runner,
            launchd_root=self.launchd_root,
            pixi=self.pixi,
            installed=installed,
            probe=self.probe,
        )

    def _drain(self, state: ClusterState) -> None:
        with self.client_factory(self.socket) as client:
            client.drain(state)

    def reconfigure(
        self,
        project: Path,
        config: InventoryInput | None = None,
        *,
        editor: Callable[[Path], None] | None = None,
        config_factory: Callable[[], InventoryInput] | None = None,
        timeout: float = 90,
    ) -> dict[str, Any]:
        self.authorize()
        with management_lock(self.state_root):
            require_no_retirement(self.state_root)
            if config is not None and config_factory is not None:
                raise ValueError("Provide config or config_factory, not both")
            original_id = load_config(self.state_root).cluster_id
            # Editing or validation failure keeps the persisted incomplete marker.
            invalidate_config(self.state_root)
            self._drain(ClusterState.RECONFIGURING)
            target = self.state_root / "cluster.yaml"
            if editor:
                target.chmod(0o600)
                editor(target)
            if config_factory:
                config = config_factory()
            updated = inventory(config) if config is not None else load_config(self.state_root)
            if updated.cluster_id not in {None, original_id}:
                raise PlatformError(
                    "CLUSTER_ID_CONFLICT", "Reconfiguration cannot change the registered UUID"
                )
            updated.cluster_id = original_id
            write_config(self.state_root, updated)
            self._install(project, installed=True)
            return wait_ready(self.socket, timeout=timeout, client_factory=self.client_factory)

    def restart(self) -> None:
        self.authorize()
        with management_lock(self.state_root):
            require_no_retirement(self.state_root)
            self._drain(ClusterState.RESTARTING)
            self.runner(["/sbin/shutdown", "-r", "now"])

    def destroy(self, *, scheduler: str | None = None) -> ClusterSnapshot:
        self.authorize()
        with management_lock(self.state_root):
            if scheduler:
                set_unregister_origin(self.state_root, scheduler)
            return destroy(self)
