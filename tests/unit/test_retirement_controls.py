"""Native queries fail closed; legacy credentials can record an explicit public origin."""

import json
import plistlib
import subprocess
from unittest.mock import patch

import pytest
from fl.macos.enrollment import set_unregister_origin
from fl.macos.launchd import RETENTION_LABEL, Installation, ensure_retention, loaded
from fl_agent.credentials import load_credentials
from fl_agent.retired_state import prepare_root
from fl_common.errors import PlatformError
from fl_common.files import atomic_write

from tests.native_macos import Launchd


@pytest.mark.parametrize("code,expected", [(0, True), (113, False), (5, None)])
def test_launchd_registration_query_distinguishes_absence_and_failure(code, expected):
    with patch("fl.macos.provisioning.subprocess.run") as command:
        command.return_value = subprocess.CompletedProcess([], code)
        if expected is None:
            with pytest.raises(PlatformError) as error:
                loaded(RETENTION_LABEL)
            assert error.value.code == "LAUNCHD_QUERY_FAILED"
        else:
            assert loaded(RETENTION_LABEL) is expected
        command.assert_called_once_with(
            ["/bin/launchctl", "print", f"system/{RETENTION_LABEL}"],
            capture_output=True,
            timeout=10,
            check=False,
        )


def test_retention_plist_has_native_interval_and_no_agent_or_network_arguments(tmp_path):
    root = prepare_root(tmp_path / "state")
    plan = Installation(project=tmp_path / "project with spaces", pixi="/opt/pixi/bin/pixi")
    document = plistlib.loads(plan.retention_plist(root))
    assert document["ProgramArguments"] == [
        plan.pixi,
        "run",
        "--locked",
        "--manifest-path",
        str(plan.project / "pixi.toml"),
        "fl-retention",
        "--retired-root",
        str(root),
    ]
    assert document["RunAtLoad"] and document["StartInterval"] == 300
    assert "KeepAlive" not in document and document["Umask"] == 0o077
    assert document["StandardErrorPath"] == str(root / "logs" / "retention.stderr.log")
    calls = []
    native = Launchd(calls, agent_registered=False)
    ensure_retention(plan, root, tmp_path / "launchd", native.run, native.loaded)
    ensure_retention(plan, root, tmp_path / "launchd", native.run, native.loaded)
    assert len([call for call in calls if call[1] == "bootstrap"]) == 1


def test_legacy_unregister_origin_update_preserves_secret_and_private_api(tmp_path):
    token = "original-agent-token-secret"
    path = tmp_path / "credentials.json"
    atomic_write(
        path,
        json.dumps(
            {
                "agent_token": token,
                "scheduler_url": "https://agent.scheduler.test",
            }
        ).encode(),
    )
    set_unregister_origin(tmp_path, "https://scheduler.test/")
    credentials = load_credentials(path)
    assert credentials.agent_token.get_secret_value() == token
    assert credentials.scheduler_url == "https://agent.scheduler.test"
    assert credentials.enrollment_url == "https://scheduler.test"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        set_unregister_origin(tmp_path, "http://scheduler.test")
    assert path.read_bytes() == before and path.stat().st_mode & 0o777 == 0o600


def test_native_root_aliases_preserve_mac_var_run_semantics(tmp_path):
    from fl.macos.provisioning import LABEL, launchd_plist

    state, runtime = tmp_path / "state", tmp_path / "private-run"
    state.mkdir()
    runtime.mkdir()
    alias = tmp_path / "run"
    alias.symlink_to(runtime, target_is_directory=True)
    path = tmp_path / "agent.plist"
    atomic_write(path, launchd_plist(tmp_path, state, alias, "/opt/pixi/bin/pixi"), mode=0o644)
    plan = Installation.from_agent(path, state, runtime)
    assert plan.project == tmp_path
    document = plistlib.loads(path.read_bytes())
    document["Label"] = LABEL
    document["ProgramArguments"][document["ProgramArguments"].index("--state-root") + 1] = str(
        tmp_path / "other-cluster"
    )
    atomic_write(path, plistlib.dumps(document), mode=0o644)
    with pytest.raises(PlatformError) as error:
        Installation.from_agent(path, state, runtime)
    assert error.value.code == "LAUNCHD_CONFIG"


def test_destroy_rejects_another_daemons_identity_before_drain(tmp_path, config):
    from uuid import uuid4

    import httpx
    from fl import Cluster, ClusterSetup
    from fl.macos.provisioning import LABEL, launchd_plist
    from fl_agent.configuration import config_confirmed, confirm_config, write_config
    from fl_common.models.base import utcnow

    config.cluster_id = uuid4()
    state, runtime = tmp_path / "state", tmp_path / "run"
    write_config(state, config)
    confirm_config(state)
    launchd = tmp_path / "launchd"
    atomic_write(
        launchd / f"{LABEL}.plist",
        launchd_plist(tmp_path, state, runtime, "/opt/pixi/bin/pixi"),
        mode=0o644,
    )
    different = config.model_copy(update={"cluster_id": uuid4()}, deep=True)
    snapshot = {
        "cluster": different.model_dump(mode="json"),
        "state": "READY",
        "timestamp": utcnow().isoformat(),
        "boards": [],
        "jobs": [],
        "queues": {},
        "artifacts": [],
        "last_event_sequence": 0,
        "system": {"cpu": 0, "memory": 0, "disk_free": 1, "disk_total": 1},
    }
    requests = []

    def rpc(request):
        requests.append(request.method)
        return httpx.Response(200, json=snapshot)

    client = httpx.Client(transport=httpx.MockTransport(rpc), base_url="http://fl-agent")
    setup = ClusterSetup(
        state,
        runtime,
        authorize=lambda: None,
        launchd_root=launchd,
        client_factory=lambda _: Cluster(client),
        runner=lambda _: pytest.fail("No native effects"),
    )
    with pytest.raises(PlatformError) as error:
        setup.destroy()
    assert error.value.code == "CLUSTER_ID_CONFLICT"
    assert requests == ["GET"] and client.is_closed and config_confirmed(state)
    assert not (prepare_root(state) / "pending-destroy.json").exists()
