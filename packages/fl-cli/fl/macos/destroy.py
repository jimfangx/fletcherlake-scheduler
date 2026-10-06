"""Retryable permanent retirement, with final-state acceptance before native teardown."""

from pathlib import Path
from typing import TYPE_CHECKING

from fl_agent.configuration import invalidate_config, load_config
from fl_agent.retired_state import prepare_root, protected
from fl_agent.retirement import Retirement, archive_state, completed_snapshot, finish
from fl_common.errors import PlatformError
from fl_common.files import fsync_directory
from fl_common.models import ClusterState
from fl_common.models.scheduler import ClusterSnapshot

from .enrollment import unregister
from .launchd import Installation, ensure_retention
from .provisioning import LABEL
from .retirement import stopped_agent

if TYPE_CHECKING:
    from .management import ClusterSetup


def capture(setup: "ClusterSetup", root: Path) -> Retirement:
    plan = Installation.from_agent(
        setup.launchd_root / f"{LABEL}.plist",
        setup.state_root,
        setup.runtime_root,
    )
    expected = load_config(setup.state_root).cluster_id
    with setup.client_factory(setup.socket) as client:
        current = ClusterSnapshot.model_validate(client.status())
        if expected is None or current.cluster.cluster_id != expected:
            raise PlatformError("CLUSTER_ID_CONFLICT", "Local socket belongs to another cluster")
        client.drain(ClusterState.DESTROYED)
        snapshot = ClusterSnapshot.model_validate(client.status())
    if (
        snapshot.state != ClusterState.DESTROYED
        or snapshot.cluster.cluster_id != expected
        or any(not job.state.terminal for job in snapshot.jobs)
        or any(snapshot.queues.values())
        or any(board.active_job for board in snapshot.boards)
    ):
        raise PlatformError("CLUSTER_NOT_DRAINED", "Daemon did not report a final drained snapshot")
    receipt = Retirement(
        state_root=setup.state_root,
        runtime_root=setup.runtime_root,
        snapshot=snapshot,
        installation=plan,
    )
    receipt.save(root)
    invalidate_config(setup.state_root)
    return receipt


def destroy(setup: "ClusterSetup") -> ClusterSnapshot:
    root = prepare_root(setup.state_root)
    receipt = Retirement.load(root)
    if receipt is None and not setup.state_root.exists():
        completed = completed_snapshot(root)
        if completed is not None:
            return completed
    receipt = receipt or capture(setup, root)
    ensure_retention(
        Installation.model_validate(receipt.installation.model_dump()),
        root,
        setup.launchd_root,
        setup.runner,
        setup.probe,
    )
    target = receipt.archive(root)
    # Atomic rename may have completed just before a process died or lost its response.
    if target.exists() and not setup.state_root.exists():
        protected(target / "snapshot.json")
        final = ClusterSnapshot.model_validate_json((target / "snapshot.json").read_bytes())
        if final != receipt.snapshot:
            raise PlatformError(
                "ARCHIVE_CONFLICT", "Archived snapshot differs from retirement intent"
            )
        finish(root)
        return final
    if receipt.phase == "DRAINED":
        with setup.http_factory() as http:
            unregister(setup.state_root, receipt.snapshot, client=http)
        receipt.phase = "UNREGISTERED"
        receipt.save(root)
    if setup.probe(LABEL):
        setup.runner(["/bin/launchctl", "bootout", f"system/{LABEL}"])
    with stopped_agent(setup.state_root, setup.runtime_root):
        if setup.probe(LABEL):
            raise PlatformError("AGENT_STILL_RUNNING", "Agent remains registered with launchd")
        if receipt.phase != "LOGGED_OUT":
            setup.network_factory().logout()
        (setup.launchd_root / f"{LABEL}.plist").unlink(missing_ok=True)
        fsync_directory(setup.launchd_root)
        receipt.phase = "LOGGED_OUT"
        receipt.save(root)
        archive_state(root, receipt)
    finish(root)
    return receipt.snapshot
