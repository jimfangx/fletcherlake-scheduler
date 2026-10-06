"""Headless Textual interaction, read-only local HTTP, and failed refresh recovery."""

import asyncio
import threading
from datetime import timedelta

import httpx
from fl import Cluster
from fl_cli.dashboard import Dashboard
from fl_cli.dashboard.details import JobDetails
from fl_cli.dashboard.formatting import timestamp
from textual.widgets import DataTable, ProgressBar, Static, TabbedContent

from tests.dashboard_snapshot import snapshot


async def refresh(app):
    app.action_refresh_snapshot()
    await app.workers.wait_for_complete()


async def test_dashboard_tables_metrics_details_and_stale_recovery(config, tmp_path):
    data, requests = snapshot(config), []
    controls = {"fail": False}

    def local(request):
        requests.append((request.method, str(request.url)))
        if controls["fail"]:
            raise httpx.ConnectError("private error detail", request=request)
        return httpx.Response(200, json=data.model_dump(mode="json"))

    with Cluster(
        httpx.Client(base_url="http://fl-agent", transport=httpx.MockTransport(local))
    ) as api:
        app = Dashboard(api)
        async with app.run_test(size=(130, 42)) as pilot:
            await app.workers.wait_for_complete()
            app.refresh_timer.pause()
            jobs = app.query_one("#jobs", DataTable)
            assert jobs.get_row(str(data.jobs[0].spec.job_id))[0].plain == "[bold]jim"
            assert app.query_one("#cpu-bar", ProgressBar).progress == 27
            assert app.query_one("#ram-bar", ProgressBar).progress == 61
            assert app.query_one("#disk-bar", ProgressBar).progress == 42.3
            assert "Disconnected" in str(app.query_one("#scheduler-link", Static).render())
            app.query_one(TabbedContent).active = "jobs-tab"
            jobs.focus()
            jobs.move_cursor(row=1)
            data.jobs.reverse()
            await refresh(app)
            assert jobs.ordered_rows[jobs.cursor_row].key.value == str(data.jobs[0].spec.job_id)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, JobDetails)
            assert app.screen.job.spec.job_id == data.jobs[0].spec.job_id
            await refresh(app)
            assert isinstance(app.screen, JobDetails)
            await pilot.press("escape")
            controls["fail"] = True
            await refresh(app)
            assert jobs.row_count == 2 and app.snapshot is not None
            banner = str(app.query_one("#connection", Static).render())
            assert "Agent unavailable" in banner and timestamp(data.timestamp) in banner
            assert "private error detail" not in banner
            controls["fail"] = False
            data.timestamp += timedelta(seconds=2)
            await refresh(app)
            assert app.snapshot.timestamp == data.timestamp
            assert "Agent unavailable" not in str(app.query_one("#connection", Static).render())
            app.save_screenshot("dashboard.svg", path=str(tmp_path))
            app.query_one(TabbedContent).active = "system-tab"
            await pilot.pause()
            app.save_screenshot("system.svg", path=str(tmp_path))
            await pilot.press("q")
    assert all(method == "GET" and url == "http://fl-agent/v1/status" for method, url in requests)


async def test_dashboard_reads_real_daemon_state_without_hardware_actions(service_factory):
    from fl_agent.api import create_app
    from fl_agent.hardware.mock import MockBehavior, MockBoardBackend
    from fl_common.models import JobConfig, ResourceConstraints

    from tests.connected import until

    backend = MockBoardBackend(MockBehavior(hang=True))
    service = service_factory({"board-0": backend})
    await service.start()
    loop, requests = asyncio.get_running_loop(), []

    async def local(request):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)), base_url="http://fl-agent"
        ) as client:
            return await client.request(request.method, request.url.path)

    def bridge(request):
        requests.append((request.method, request.url.path))
        response = asyncio.run_coroutine_threadsafe(local(request), loop).result(5)
        return httpx.Response(response.status_code, content=response.content)

    with Cluster(
        httpx.Client(base_url="http://fl-agent", transport=httpx.MockTransport(bridge))
    ) as api:
        app = Dashboard(api)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            app.refresh_timer.pause()
            assert app.query_one("#boards", DataTable).row_count == 3
            job = await service.submit(
                JobConfig(resource_constraints=ResourceConstraints(board="board-0"))
            )
            await until(lambda: service.db.get(job.spec.job_id).state == "RUNNING")
            await refresh(app)
            boards = app.query_one("#boards", DataTable)
            assert boards.get_row("board-0")[3].plain == "RUNNING"
            assert boards.get_row("board-0")[4].plain == str(job.spec.job_id)
            # The executing job continues polling UART independently of dashboard reads.
            calls = [operation for operation in backend.calls if operation != "read_uart"]
            await refresh(app)
            assert [operation for operation in backend.calls if operation != "read_uart"] == calls
            await pilot.press("q")
    assert all(method == "GET" and path == "/v1/status" for method, path in requests)


async def test_slow_refresh_does_not_overlap_socket_requests(config):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def local(request):
        calls.append(request.url.path)
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=snapshot(config).model_dump(mode="json"))

    with Cluster(
        httpx.Client(base_url="http://fl-agent", transport=httpx.MockTransport(local))
    ) as api:
        app = Dashboard(api)
        async with app.run_test() as pilot:
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                for _ in range(5):
                    app.action_refresh_snapshot()
                assert calls == ["/v1/status"]
            finally:
                release.set()
            await app.workers.wait_for_complete()
            await pilot.press("q")
