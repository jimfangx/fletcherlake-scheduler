"""The specified outage durations use wall time and real TCP/WebSocket reconnects."""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from fl_common.files import atomic_write
from fl_common.models.scheduler import Principal, Role

from tests.partition_proxy import FaultProxy
from tests.partition_scenario import exercise, prepare


async def test_ten_second_one_minute_and_ten_minute_partitions(scheduler_db, connected_agents):
    if os.environ.get("FL_TEST_LONG_PARTITIONS") != "1":
        pytest.skip("Set FL_TEST_LONG_PARTITIONS=1 for real 10s/60s/600s acceptance (~11 minutes)")
    server, services = connected_agents
    owner = Principal(email="alice@berkeley.edu", subject="alice", role=Role.USER)
    async with AsyncExitStack() as stack:
        proxies = []
        for _ in services:
            proxy = await FaultProxy(server.port).start()
            stack.push_async_callback(proxy.stop)
            proxies.append(proxy)
        durations = (10, 60, 600)
        scenarios = await asyncio.gather(
            *[
                prepare(service, proxy, scheduler_db, owner, duration)
                for service, proxy, duration in zip(services, proxies, durations, strict=True)
            ]
        )
        async with asyncio.timeout(800):
            results = await asyncio.gather(
                *[
                    exercise(service, proxy, scheduler_db, owner, duration, scenario)
                    for service, proxy, duration, scenario in zip(
                        services, proxies, durations, scenarios, strict=True
                    )
                ]
            )
        assert server.server.started and not server.task.done()
        if report := os.environ.get("FL_PARTITION_REPORT"):
            atomic_write(Path(report), json.dumps(results, indent=2).encode())
