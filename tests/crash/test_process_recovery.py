"""Kill -9 tests prove queue durability across abrupt process death, not just close()."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from fl_agent.configuration import confirm_config, write_config
from fl_common.models import JobConfig, ResourceConstraints, ShmooConfig, SweepRange

from tests.crash.helpers import HARNESS, poll


@pytest.mark.parametrize(
    "phase",
    [
        "QUEUED",
        "PROGRAMMING_FPGA",
        "PROGRAMMING_SOC",
        "RUNNING",
        "CANCELING",
        "SHMOO_SAMPLE",
    ],
)
def test_hard_kill_preserves_queue_and_reconciles_active_job(
    tmp_path: Path,
    config,
    input_files,
    phase: str,
) -> None:
    root = tmp_path / "state"
    write_config(root, config)
    confirm_config(root)
    with tempfile.TemporaryDirectory(prefix="fl-crash-") as runtime_name:
        runtime = Path(runtime_name)
        with (tmp_path / "daemon.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(HARNESS),
                    str(root),
                    str(runtime),
                    json.dumps(
                        {
                            "programming_seconds": 0.5,
                            "run_seconds": 1 if phase == "SHMOO_SAMPLE" else 30,
                        }
                    ),
                ],
                stdout=log,
                stderr=log,
            )
            client = httpx.Client(
                transport=httpx.HTTPTransport(uds=str(runtime / "agent.sock")),
                base_url="http://agent",
                timeout=2,
            )
            try:
                poll(client, process, "/v1/status", lambda data: data["state"] == "READY")
                job_config = JobConfig(
                    **input_files,
                    resource_constraints=ResourceConstraints(board="board-0"),
                    shmoo=ShmooConfig(voltage=SweepRange(low=0.7, high=0.9, step=0.05))
                    if phase == "SHMOO_SAMPLE"
                    else None,
                ).model_dump(mode="json")
                first_response = client.post("/v1/jobs", json=job_config)
                first_response.raise_for_status()
                active_id = first_response.json()["spec"]["job_id"]
                queued_response = client.post("/v1/jobs", json=job_config)
                queued_response.raise_for_status()
                queued_id = queued_response.json()["spec"]["job_id"]
                if phase == "QUEUED":
                    poll(
                        client,
                        process,
                        f"/v1/jobs/{queued_id}",
                        lambda data: data["state"] == phase,
                    )
                elif phase == "SHMOO_SAMPLE":
                    poll(
                        client,
                        process,
                        "/v1/events",
                        lambda data: any(
                            event["job_id"] == active_id and event["type"] == "SHMOO_SAMPLE"
                            for event in data
                        ),
                    )
                elif phase == "CANCELING":
                    poll(
                        client,
                        process,
                        f"/v1/jobs/{active_id}",
                        lambda data: data["state"] == "RUNNING",
                    )
                    assert client.post(f"/v1/jobs/{active_id}/cancel").json()["state"] == phase
                else:
                    poll(
                        client,
                        process,
                        f"/v1/jobs/{active_id}",
                        lambda data: data["state"] == phase,
                    )
                process.kill()
                assert process.wait(timeout=5) == -9
                # These fingerprints model stale runtime contents on a daemon restart.
                (runtime / "stale.bitstream.sha256").write_text("stale")
                process = subprocess.Popen(
                    [sys.executable, str(HARNESS), str(root), str(runtime), "{}"],
                    stdout=log,
                    stderr=log,
                )
                poll(client, process, "/v1/status", lambda data: data["state"] == "READY")
                expected = "CANCELED" if phase == "CANCELING" else "INTERRUPTED"
                active = poll(
                    client, process, f"/v1/jobs/{active_id}", lambda data: data["state"] == expected
                )
                assert active["finished_at"] is not None
                if phase == "SHMOO_SAMPLE":
                    response = client.get(f"/v1/jobs/{active_id}/artifacts/results")
                    response.raise_for_status()
                    assert response.json()["shmoo"]["samples"][0]["value"] == 0.9
                poll(
                    client,
                    process,
                    f"/v1/jobs/{queued_id}",
                    lambda data: data["state"] == "SUCCEEDED",
                )
                snapshot = client.get("/v1/status").json()
                assert not (runtime / "stale.bitstream.sha256").exists()
                assert all(record["expires_at"] is not None for record in snapshot["artifacts"])
                assert {record["job_id"] for record in snapshot["artifacts"]} == {
                    active_id,
                    queued_id,
                }
            finally:
                client.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
