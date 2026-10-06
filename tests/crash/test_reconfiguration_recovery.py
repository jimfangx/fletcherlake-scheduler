"""SIGKILL during management preserves identity, confirmation fences and durable jobs."""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fl import ClusterSetup
from fl.macos.provisioning import LABEL, launchd_plist
from fl_agent.collateral import CollateralStore
from fl_agent.configuration import config_confirmed, confirm_config, load_config, write_config
from fl_agent.db import AgentDB
from fl_common.files import atomic_write, sha256_file
from fl_common.models import JobConfig, ResourceConstraints
from fl_common.models.artifact import ARTIFACT_FILENAMES

from tests.crash.helpers import HARNESS, poll


@pytest.mark.parametrize("boundary", ["editor", "sync", "bootout"])
def test_sigkill_reconfiguration_recovers_without_losing_queue(
    tmp_path, config, input_files, boundary
):
    config.cluster_id = uuid4()
    state = tmp_path / "state with spaces"
    write_config(state, config)
    confirm_config(state)
    (tmp_path / "pixi.toml").write_text("placeholder")
    atomic_write(tmp_path / "registered", b"yes")
    manager = None
    with (
        tempfile.TemporaryDirectory(prefix="fl-reconfigure-") as runtime_name,
        (tmp_path / "agent.log").open("wb") as log,
        (tmp_path / "management.log").open("wb") as management_log,
    ):
        runtime = Path(runtime_name)

        def start(behavior):
            return subprocess.Popen(
                [sys.executable, str(HARNESS), str(state), str(runtime), json.dumps(behavior)],
                stdout=log,
                stderr=log,
            )

        daemon = start({"run_seconds": 30})
        client = httpx.Client(
            transport=httpx.HTTPTransport(uds=str(runtime / "agent.sock")),
            base_url="http://agent",
            timeout=2,
        )
        try:
            poll(client, daemon, "/v1/status", lambda data: data["state"] == "READY")
            job = JobConfig(
                **input_files, resource_constraints=ResourceConstraints(board="board-0")
            ).model_dump(mode="json")
            active = client.post("/v1/jobs", json=job)
            active.raise_for_status()
            active_id = active.json()["spec"]["job_id"]
            poll(client, daemon, f"/v1/jobs/{active_id}", lambda data: data["state"] == "RUNNING")
            queued = client.post("/v1/jobs", json=job)
            queued.raise_for_status()
            queued_id = queued.json()["spec"]["job_id"]
            atomic_write(
                tmp_path / "launchd" / f"{LABEL}.plist",
                launchd_plist(tmp_path, state, runtime, "/opt/pixi/bin/pixi"),
                mode=0o644,
            )
            manager = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("reconfiguration_process.py")),
                    str(tmp_path),
                    str(state),
                    str(runtime),
                    boundary,
                    str(daemon.pid),
                ],
                stdout=management_log,
                stderr=management_log,
            )
            deadline = time.monotonic() + 15
            while not (tmp_path / "checkpoint").exists():
                assert manager.poll() is None, "Management exited before its crash boundary"
                assert time.monotonic() < deadline, "Management did not reach its crash boundary"
                time.sleep(0.01)
            manager.kill()
            assert manager.wait(timeout=5) == -9
            assert config_confirmed(state) == (boundary == "bootout")
            assert load_config(state).cluster_id == config.cluster_id
            if boundary != "bootout":
                daemon.kill()
            daemon.wait(timeout=15)
            db = AgentDB(state / "agent.db")
            try:
                assert {str(item.spec.job_id): item.state for item in db.jobs()} == {
                    active_id: "INTERRUPTED",
                    queued_id: "QUEUED",
                }
                retained = CollateralStore(state / "jobs", db).records()
                assert retained and all(
                    (item.expires_at is not None) == (str(item.job_id) == active_id)
                    for item in retained
                )
            finally:
                db.close()
            if boundary != "bootout":
                daemon = start({})
                poll(
                    client,
                    daemon,
                    "/v1/status",
                    lambda data: data["state"] == "CONFIGURATION_INCOMPLETE",
                )
                assert client.get(f"/v1/jobs/{queued_id}").json()["state"] == "QUEUED"
                assert client.post("/v1/jobs", json=job).status_code == 409

            def runner(argv):
                nonlocal daemon
                if argv[1] == "bootout":
                    assert (tmp_path / "registered").read_text() == "yes"
                    daemon.terminate()
                    daemon.wait(timeout=15)
                    atomic_write(tmp_path / "registered", b"no")
                elif argv[1] == "bootstrap":
                    assert (tmp_path / "registered").read_text() == "no"
                    daemon = start({})
                    atomic_write(tmp_path / "registered", b"yes")
                else:
                    assert argv[1] in {"install", "kickstart"}

            setup = ClusterSetup(
                state,
                runtime,
                authorize=lambda: None,
                runner=runner,
                pixi="/opt/pixi/bin/pixi",
                launchd_root=tmp_path / "launchd",
                probe=lambda _: (tmp_path / "registered").read_text() == "yes",
            )
            assert setup.confirm(tmp_path)["state"] == "READY"
            poll(client, daemon, f"/v1/jobs/{queued_id}", lambda data: data["state"] == "SUCCEEDED")
            assert client.get(f"/v1/jobs/{active_id}").json()["state"] == "INTERRUPTED"
            events = client.get("/v1/events").json()
            assert sum(item["type"] == "JOB_RUNNING" for item in events) == 2
            assert config_confirmed(state) and load_config(state).cluster_id == config.cluster_id
            for item in retained:
                path = state / "jobs" / str(item.job_id) / ARTIFACT_FILENAMES[item.ref.kind]
                assert sha256_file(path) == (item.ref.sha256, item.ref.size_bytes)
            current = client.get("/v1/status").json()["artifacts"]
            for item in retained:
                if str(item.job_id) == active_id:
                    assert item.model_dump(mode="json") in current
        finally:
            client.close()
            if manager is not None and manager.poll() is None:
                manager.kill()
                manager.wait(timeout=5)
            if daemon.poll() is None:
                daemon.terminate()
                daemon.wait(timeout=15)
