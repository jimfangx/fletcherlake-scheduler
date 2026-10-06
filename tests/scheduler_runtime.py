"""Restartable disposable scheduler process with verified local HTTPS readiness."""

import asyncio
import json
import socket
import ssl
import subprocess
import sys
from pathlib import Path

import httpx
from fl_common.files import atomic_write
from fl_scheduler.db.models import Cluster

from tests.dashboard_https import certificate


def projection(db, cluster_id):
    with db.transaction() as session:
        cluster = session.get(Cluster, cluster_id)
        return cluster.event_sequence, cluster.current_session


class SchedulerProcess:
    def __init__(self, db, root):
        self.root = root
        root.mkdir(mode=0o700)
        certificate(root)
        self.context = ssl.create_default_context(cafile=str(root / "server.pem"))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.origin = f"https://127.0.0.1:{port}"
        atomic_write(
            root / "scheduler.json",
            json.dumps(
                {
                    "database_url": db.engine.url.render_as_string(hide_password=False),
                    "origin": self.origin,
                    "port": port,
                }
            ).encode(),
        )
        atomic_write(root / "scheduler.log", b"")
        self.process = None

    def start(self):
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("Scheduler process is already running")
        with (self.root / "scheduler.log").open("ab") as log:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tests.crash.scheduler_process",
                    str(self.root),
                ],
                cwd=Path(__file__).resolve().parent.parent,
                stdout=log,
                stderr=log,
            )

    async def ready(self):
        async with (
            asyncio.timeout(20),
            httpx.AsyncClient(verify=self.context, trust_env=False, timeout=1) as client,
        ):
            while True:
                assert self.process.poll() is None, "Scheduler exited before HTTPS readiness"
                try:
                    response = await client.get(self.origin + "/healthz")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)

    def stop(self, *, kill=False):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.kill() if kill else self.process.terminate()
        self.process.wait(timeout=15)
