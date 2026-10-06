"""Bounded readiness polling for subprocess agents over their real Unix socket API."""

import subprocess
import time
from pathlib import Path

import httpx

HARNESS = Path(__file__).with_name("agent_process.py")


def poll(client: httpx.Client, process: subprocess.Popen[bytes], path: str, predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert process.poll() is None, "Agent exited before reaching the expected state"
        try:
            response = client.get(path)
            response.raise_for_status()
            data = response.json()
            if predicate(data):
                return data
        except httpx.HTTPError:
            pass
        time.sleep(0.01)
    raise AssertionError(f"Agent did not reach expected state at {path}")
