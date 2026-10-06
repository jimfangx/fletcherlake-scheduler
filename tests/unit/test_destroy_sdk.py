"""Unregistration acknowledgement is required before native teardown removes recovery data."""

import json
from uuid import uuid4

import httpx
import pytest
from fl import Cluster, ClusterSetup
from fl.macos.launchd import RETENTION_LABEL
from fl.macos.provisioning import LABEL, launchd_plist
from fl_agent.collateral import CollateralStore
from fl_agent.configuration import config_confirmed, confirm_config, write_config
from fl_agent.db import AgentDB
from fl_agent.retired_state import retired_root
from fl_common.errors import PlatformError
from fl_common.files import atomic_write
from fl_common.models import JobConfig, JobSpec
from fl_common.models.base import utcnow
from fl_common.models.scheduler import ClusterSnapshot

from tests.native_macos import Launchd


@pytest.mark.parametrize("accepted", [False, True])
def test_destroy_preserves_recovery_data_until_final_snapshot_accepted(tmp_path, config, accepted):
    config.cluster_id = uuid4()
    state = tmp_path / "state"
    write_config(state, config)
    state.chmod(0o700)
    confirm_config(state)
    atomic_write(
        state / "credentials.json",
        json.dumps(
            {
                "scheduler_url": "https://agent.scheduler.test",
                "agent_token": "protected-agent-token",
            }
        ).encode(),
    )
    db = AgentDB(state / "agent.db")
    db.initialize_boards(["board-0"])
    spec = JobSpec.from_config(JobConfig(), "alice")
    db.create(spec, "board-0")
    store = CollateralStore(state / "jobs", db)
    store.save_spec(spec)
    final = db.cancel(spec.job_id)
    store.path(spec.job_id, "results").write_text("retained collateral")
    store.finalize(spec, final.finished_at)
    db.set_metadata("cluster_state", "DESTROYED")
    db.close()
    launchd = tmp_path / "launchd"
    launchd.mkdir()
    plist = launchd / f"{LABEL}.plist"
    plist.write_bytes(launchd_plist(tmp_path, state, tmp_path / "run", "/opt/bin/pixi"))
    operations, clients = [], []
    native = Launchd(operations)
    preflight = [
        "/opt/bin/pixi",
        "run",
        "--locked",
        "--manifest-path",
        str(tmp_path / "pixi.toml"),
        "fl-retention",
        "--retired-root",
        str(retired_root(state)),
        "--check",
    ]
    snapshot = {
        "schema_version": 1,
        "cluster": config.model_dump(mode="json"),
        "state": "DESTROYED",
        "timestamp": utcnow().isoformat(),
        "boards": [],
        "jobs": [final.model_dump(mode="json")],
        "queues": {},
        "artifacts": [],
        "last_event_sequence": 0,
        "system": {"cpu": 0, "memory": 0, "disk_free": 1, "disk_total": 1},
    }

    def local_rpc(request):
        operations.append(request.url.path)
        if request.url.path.endswith("/drain"):
            assert json.loads(request.content) == {"state": "DESTROYED"}
            return httpx.Response(200, json={"state": "DESTROYED"})
        return httpx.Response(200, json=snapshot)

    def unregister(request):
        operations.append("unregister")
        assert request.url.host == "agent.scheduler.test"
        assert request.url.path == f"/api/agents/{config.cluster_id}/unregister"
        assert request.headers["Authorization"] == "Bearer protected-agent-token"
        submitted = ClusterSnapshot.model_validate_json(request.content)
        assert submitted == ClusterSnapshot.model_validate(snapshot)
        assert plist.is_file() and (state / "credentials.json").is_file()
        return httpx.Response(
            202 if accepted else 503,
            json={}
            if accepted
            else {
                "code": "RETRY_UNREGISTER",
                "message": "Try again",
            },
        )

    def client(transport):
        http = httpx.Client(transport=httpx.MockTransport(transport), base_url="http://fl-agent")
        clients.append(http)
        return http

    class Network:
        def logout(self):
            operations.append("logout")

    setup = ClusterSetup(
        state,
        tmp_path / "run",
        authorize=lambda: None,
        launchd_root=launchd,
        runner=native.run,
        probe=native.loaded,
        client_factory=lambda _: Cluster(client(local_rpc)),
        http_factory=lambda: client(unregister),
        network_factory=Network,
    )
    if accepted:
        result = setup.destroy()
        assert result.cluster.cluster_id == config.cluster_id
        assert operations == [
            "/v1/status",
            "/v1/cluster/drain",
            "/v1/status",
            preflight,
            ["/bin/launchctl", "bootstrap", "system", str(launchd / f"{RETENTION_LABEL}.plist")],
            "unregister",
            ["/bin/launchctl", "bootout", f"system/{LABEL}"],
            "logout",
        ]
        assert not plist.exists() and not (state / "cluster.yaml").exists()
        assert not (state / "credentials.json").exists()
    else:
        with pytest.raises(PlatformError) as error:
            setup.destroy()
        assert error.value.code == "RETRY_UNREGISTER"
        assert operations == [
            "/v1/status",
            "/v1/cluster/drain",
            "/v1/status",
            preflight,
            ["/bin/launchctl", "bootstrap", "system", str(launchd / f"{RETENTION_LABEL}.plist")],
            "unregister",
        ]
        assert plist.is_file() and (state / "credentials.json").is_file()
        assert (state / "cluster.yaml").is_file()
    assert all(http.is_closed for http in clients)
    location = retired_root(state) / str(config.cluster_id) if accepted else state
    assert not config_confirmed(state)
    retained = AgentDB(location / "agent.db")
    try:
        assert retained.get(spec.job_id) == final
    finally:
        retained.close()
    assert (
        location / "jobs" / str(spec.job_id) / "results.json"
    ).read_text() == "retained collateral"
    assert (retired_root(state) / "pending-destroy.json").exists() != accepted
