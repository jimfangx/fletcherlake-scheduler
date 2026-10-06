"""Synchronous local SDK shared by the CLI and interactive Python workflows."""

from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fl_common.models import ClusterState, JobConfig, JobRecord


class Cluster:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    @classmethod
    def local(cls, socket: Path = Path("/var/run/fl/agent.sock")) -> "Cluster":
        return cls(
            httpx.Client(
                transport=httpx.HTTPTransport(uds=str(socket)),
                base_url="http://fl-agent",
                timeout=30,
            )
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Cluster":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def status(self) -> dict[str, Any]:
        response = self.client.get("/v1/status")
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    def submit(self, config: str | Path | JobConfig, **overrides: object) -> JobRecord:
        if isinstance(config, str | Path):
            parsed = JobConfig.from_yaml(Path(config), overrides)
        else:
            parsed = JobConfig.model_validate({**config.model_dump(), **overrides})
        response = self.client.post("/v1/jobs", json=parsed.model_dump(mode="json"), timeout=None)
        response.raise_for_status()
        return JobRecord.model_validate(response.json())

    def jobs(self) -> list[JobRecord]:
        response = self.client.get("/v1/jobs")
        response.raise_for_status()
        return [JobRecord.model_validate(job) for job in response.json()]

    def job(self, job_id: UUID) -> JobRecord:
        response = self.client.get(f"/v1/jobs/{job_id}")
        response.raise_for_status()
        return JobRecord.model_validate(response.json())

    def kill(self, job_id: UUID) -> JobRecord:
        response = self.client.post(f"/v1/jobs/{job_id}/cancel")
        response.raise_for_status()
        return JobRecord.model_validate(response.json())

    def drain(self, state: ClusterState = ClusterState.DRAINING) -> None:
        response = self.client.post("/v1/cluster/drain", json={"state": state}, timeout=None)
        response.raise_for_status()

    def artifact(self, job_id: UUID, kind: str) -> bytes:
        response = self.client.get(f"/v1/jobs/{job_id}/artifacts/{kind}")
        response.raise_for_status()
        return response.content

    def delete_collateral(self, job_id: UUID) -> None:
        self.client.delete(f"/v1/jobs/{job_id}/artifacts").raise_for_status()
